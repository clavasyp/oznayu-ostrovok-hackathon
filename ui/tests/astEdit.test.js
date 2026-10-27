import test from 'node:test'
import assert from 'node:assert/strict'
import { applyEdits, astProblems, atomPlace, conditionChips, editOf, moveBlock, moveCondition, recordEdit } from '../src/astEdit.js'

// Разборы правилами из POST /api/search (режим fixtures), без лишних полей.
const cond = (atom_id, feature_id, text_span, extra = {}) => ({ atom_id, feature_id, text_span, op: 'eq', value: true, field: null, params: {}, binding: 'hotel', quantifier: 'all', provenance: 'explicit', mapping: 'exact', ...extra })
const base = (extra) => ({ raw_text: 'запрос', city_id: 'sochi', hard: [], soft: [], clarify: [], out_of_catalog: [], logic: [], hard_root: null, unparsed_spans: [], parsed_by: 'rules', ...extra })

const dogOrCat = () => base({ hard: [cond('a1', 'P01', 'с собакой'), cond('a2', 'P01', 'с кошкой')] })
const lateCheckIn = () => base({
  hard: [cond('a1', 'L04', 'поздний заезд', { value: '24/7' }), cond('a2', 'L04', 'поздний заезд'), cond('a3', 'L01', 'открытая аптека рядом', { op: 'lte', value: 1000 })],
  logic: [
    { node_id: 'n_a1', op: 'ATOM', atom_id: 'a1', children: [] },
    { node_id: 'n_a2', op: 'ATOM', atom_id: 'a2', children: [] },
    { node_id: 'n_a3', op: 'ATOM', atom_id: 'a3', children: [] },
    { node_id: 'or_g1', op: 'OR', atom_id: null, children: ['n_a1', 'n_a2'] },
    { node_id: 'root', op: 'AND', atom_id: null, children: ['or_g1', 'n_a3'] },
  ],
  hard_root: 'root',
})
const sunset = () => base({
  hard: [cond('a3', 'V05', 'видом на закат')],
  soft: [cond('a1', 'N02', 'тихий', { weight: 0.3333 }), cond('a2', 'N04', 'тихий', { weight: 0.1667 }), cond('a4', 'W01', 'недалеко от моря', { weight: 0.5 })],
})
const weights = (ast) => ast.soft.reduce((sum, item) => sum + item.weight, 0)

test('moving one of two conditions with the same feature keeps the other (Б2: «с собакой или с кошкой»)', () => {
  const moved = moveCondition(dogOrCat(), 'a1')
  assert.deepEqual(moved.hard.map((item) => item.atom_id), ['a2'])
  assert.deepEqual(moved.soft.map((item) => [item.atom_id, item.weight]), [['a1', 1]])
  assert.deepEqual(astProblems(moved), [])
})

test('members of an OR group cannot move; a top-level AND atom leaves the graph intact (Б2: «поздний заезд»)', () => {
  const ast = lateCheckIn()
  assert.equal(atomPlace(ast, 'a1'), 'group')
  assert.ok(moveBlock(ast, 'a1'))
  assert.equal(moveCondition(ast, 'a2'), null)
  assert.equal(atomPlace(ast, 'a3'), 'top')
  const moved = moveCondition(ast, 'a3')
  assert.deepEqual(moved.hard.map((item) => item.atom_id), ['a1', 'a2'])
  assert.deepEqual(moved.soft.map((item) => item.atom_id), ['a3'])
  assert.equal(moved.hard_root, 'root')
  assert.deepEqual(moved.logic.find((node) => node.node_id === 'root').children, ['or_g1'])
  assert.ok(!moved.logic.some((node) => node.atom_id === 'a3'))
  assert.deepEqual(astProblems(moved), [])
  // Обратно в обязательные — вне графа: движок соединит его с графом через И.
  const back = moveCondition(moved, 'a3')
  assert.deepEqual(back.hard.map((item) => item.atom_id), ['a1', 'a2', 'a3'])
  assert.deepEqual(back.soft, [])
  assert.deepEqual(astProblems(back), [])
})

test('soft weights stay a distribution after every move', () => {
  const toSoft = moveCondition(sunset(), 'a3')
  assert.ok(Math.abs(weights(toSoft) - 1) <= 0.01, `сумма ${weights(toSoft)}`)
  assert.equal(toSoft.soft.find((item) => item.atom_id === 'a3').weight, 0.25)
  assert.deepEqual(toSoft.hard, [])
  const toHard = moveCondition(sunset(), 'a4')
  assert.ok(Math.abs(weights(toHard) - 1) <= 0.01)
  assert.deepEqual(toHard.hard.map((item) => item.atom_id), ['a3', 'a4'])
  assert.ok(!('weight' in toHard.hard[1]))
  assert.deepEqual(astProblems(toHard), [])
})

test('inferred becomes explicit when the guest makes it required; partial mapping never becomes required', () => {
  const ast = base({ soft: [cond('a1', 'W01', 'у моря', { provenance: 'inferred', weight: 0.5 }), cond('a2', 'E03', 'парк рядом', { mapping: 'partial', weight: 0.5 })] })
  assert.equal(moveCondition(ast, 'a1').hard[0].provenance, 'explicit')
  assert.ok(moveBlock(ast, 'a2'))
  assert.equal(moveCondition(ast, 'a2'), null)
})

test('NOT members are locked, a lone root atom clears the graph, a broken graph locks hard atoms', () => {
  const negated = base({ hard: [cond('a1', 'P01', 'собак')], logic: [{ node_id: 'n1', op: 'ATOM', atom_id: 'a1', children: [] }, { node_id: 'not1', op: 'NOT', atom_id: null, children: ['n1'] }, { node_id: 'root', op: 'AND', atom_id: null, children: ['not1'] }], hard_root: 'root' })
  assert.equal(atomPlace(negated, 'a1'), 'group')
  const chip = conditionChips(negated)[0]
  assert.equal(chip.label, 'не собак')
  assert.ok(chip.block)
  const lone = base({ hard: [cond('a1', 'A03', 'лифт')], logic: [{ node_id: 'n1', op: 'ATOM', atom_id: 'a1', children: [] }], hard_root: 'n1' })
  const moved = moveCondition(lone, 'a1')
  assert.deepEqual([moved.logic, moved.hard_root], [[], null])
  const broken = base({ hard: [cond('a1', 'A03', 'лифт')], logic: [{ node_id: 'n1', op: 'ATOM', atom_id: 'a1', children: [] }], hard_root: 'missing' })
  assert.equal(moveCondition(broken, 'a1'), null)
})

test('OR atoms show as one chip; the edit survives a new parse with other atom ids', () => {
  const chips = conditionChips(lateCheckIn())
  assert.equal(chips.filter((chip) => chip.kind === 'required').length, 2)
  const group = chips.find((chip) => chip.group === 'OR')
  assert.deepEqual([group.label, group.conditions.length, Boolean(group.block)], ['поздний заезд', 2, true])
  const edits = recordEdit(recordEdit([], editOf(lateCheckIn().hard[2], 'soft')), editOf(sunset().hard[0], 'soft'))
  assert.equal(edits.length, 2)
  // Новый разбор того же текста с датами: номера атомов другие, правка та же.
  const reparsed = lateCheckIn()
  reparsed.hard[2] = { ...reparsed.hard[2], atom_id: 'a7' }
  reparsed.logic = reparsed.logic.map((node) => node.atom_id === 'a3' ? { ...node, atom_id: 'a7' } : node)
  const { ast, applied } = applyEdits(reparsed, edits)
  assert.equal(applied.length, 1)
  assert.deepEqual(ast.soft.map((item) => item.atom_id), ['a7'])
  assert.deepEqual(astProblems(ast), [])
  // Повторный перенос того же условия заменяет правку, а не копит их.
  assert.equal(recordEdit(edits, editOf(lateCheckIn().hard[2], 'hard')).length, 2)
})
