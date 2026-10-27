// Правка разобранного запроса в «Я понял так» (Б2): перенос одного условия между
// «обязательно» и «желательно» по atom_id. Граф logic / hard_root остаётся целым, веса soft
// дают в сумме 1, выведенное условие обязательным становится только как явное.
// Контракт AST — agents/query_ast.py и agents/search/validate.py.

const round4 = (value) => Math.round(value * 10000) / 10000

function index(ast) {
  const nodes = new Map((ast?.logic || []).map((node) => [node.node_id, node]))
  const parents = new Map()
  for (const node of nodes.values()) {
    for (const child of node.children || []) {
      if (!parents.has(child)) parents.set(child, [])
      parents.get(child).push(node)
    }
  }
  return { nodes, parents, root: ast?.hard_root ? nodes.get(ast.hard_root) : undefined }
}

function reachable(ast) {
  const { nodes, root } = index(ast)
  const seen = new Set()
  const stack = root ? [root.node_id] : []
  while (stack.length) {
    const id = stack.pop()
    const node = nodes.get(id)
    if (!node || seen.has(id)) continue
    seen.add(id)
    stack.push(...(node.children || []))
  }
  return seen
}

// Где обязательный атом стоит в графе:
// 'none' — графа нет или атома в нём нет (соединяется через И);
// 'root' — весь граф и есть этот атом; 'top' — прямой ребёнок корневого И;
// 'group' — внутри «ИЛИ», «НЕ» или вложенной группы; 'broken' — граф без корня.
export function atomPlace(ast, atomId) {
  if (!ast?.logic?.length) return 'none'
  const { nodes, parents, root } = index(ast)
  if (!root) return 'broken'
  const live = reachable(ast)
  const atomNodes = [...nodes.values()].filter((node) => node.op === 'ATOM' && node.atom_id === atomId && live.has(node.node_id))
  if (!atomNodes.length) return 'none'
  if (atomNodes.some((node) => node.node_id === root.node_id)) return 'root'
  const onlyUnderRootAnd = atomNodes.every((node) => (parents.get(node.node_id) || []).every((parent) => parent.node_id === root.node_id && root.op === 'AND'))
  return onlyUnderRootAnd ? 'top' : 'group'
}

function withoutAtom(ast, atomId) {
  const { root } = index(ast)
  const drop = new Set((ast.logic || []).filter((node) => node.op === 'ATOM' && node.atom_id === atomId).map((node) => node.node_id))
  if (root && drop.has(root.node_id)) return { logic: [], hard_root: null }
  const logic = (ast.logic || [])
    .filter((node) => !drop.has(node.node_id))
    .map((node) => (node.children || []).some((child) => drop.has(child)) ? { ...node, children: node.children.filter((child) => !drop.has(child)) } : node)
  const newRoot = logic.find((node) => node.node_id === ast.hard_root)
  if (!newRoot || (newRoot.op !== 'ATOM' && !newRoot.children.length)) return { logic: [], hard_root: null }
  return { logic, hard_root: ast.hard_root }
}

// Веса желательных условий — в сумме 1 (agents/search/validate.py), каждый в (0, 1].
export function normalizeWeights(soft) {
  if (!soft.length) return soft
  const total = soft.reduce((sum, item) => sum + (item.weight > 0 ? item.weight : 0), 0)
  return soft.map((item) => {
    const share = total > 0 ? (item.weight > 0 ? item.weight : 0) / total : 1 / soft.length
    return { ...item, weight: Math.min(1, Math.max(0.0001, round4(share || 1 / soft.length))) }
  })
}

// Почему условие нельзя перенести; null — можно.
export function moveBlock(ast, atomId) {
  if (!ast || !atomId) return 'У условия нет номера в разборе — поправьте текст запроса'
  if ((ast.hard || []).some((item) => item.atom_id === atomId)) {
    const place = atomPlace(ast, atomId)
    if (place === 'group') return 'Это часть «или» / «не» — чтобы поменять, поправьте текст запроса'
    if (place === 'broken') return 'Связь условий не разобрана — поправьте текст запроса'
    return null
  }
  const soft = (ast.soft || []).find((item) => item.atom_id === atomId)
  if (!soft) return 'Условие не найдено в разборе'
  if ((soft.mapping || 'exact') !== 'exact') return 'Понято лишь частично — обязательным это сделать нельзя'
  return null
}

// Перенос одного условия. Возвращает новый AST или null, если переносить нельзя.
export function moveCondition(ast, atomId) {
  if (moveBlock(ast, atomId)) return null
  const hardIndex = (ast.hard || []).findIndex((item) => item.atom_id === atomId)
  if (hardIndex >= 0) {
    const condition = ast.hard[hardIndex]
    const hard = ast.hard.filter((_, i) => i !== hardIndex)
    const existing = ast.soft || []
    // Новое желательное — средний вес остальных, потом всё к сумме 1: новому 1/(n+1), остальным × n/(n+1).
    const average = existing.length ? existing.reduce((sum, item) => sum + item.weight, 0) / existing.length : 1
    const soft = normalizeWeights([...existing, { ...condition, weight: average || 1 }])
    // Атом уходит из графа вместе с узлом (иначе пункт графа стал бы «не разобрали связь»).
    return { ...ast, hard, soft, ...withoutAtom(ast, atomId) }
  }
  const softIndex = (ast.soft || []).findIndex((item) => item.atom_id === atomId)
  const { weight, ...condition } = ast.soft[softIndex]
  // Обязательное — только явное (validate: выведенное обязательным быть не может): гость сам это выбрал.
  const hard = [...(ast.hard || []), { ...condition, provenance: 'explicit' }]
  const soft = normalizeWeights(ast.soft.filter((_, i) => i !== softIndex))
  return { ...ast, hard, soft }
}

// Правка гостя переживает новый поиск, пока в разборе есть то же условие (характеристика и слова).
export function editOf(condition, to) {
  return { feature_id: condition.feature_id, text_span: condition.text_span || '', to }
}

export function recordEdit(edits, edit) {
  const rest = edits.filter((item) => !(item.feature_id === edit.feature_id && item.text_span === edit.text_span))
  return [...rest, edit]
}

export function applyEdits(ast, edits) {
  let next = ast
  const applied = []
  for (const edit of edits || []) {
    const from = edit.to === 'soft' ? 'hard' : 'soft'
    const condition = (next?.[from] || []).find((item) => item.feature_id === edit.feature_id && (item.text_span || '') === edit.text_span)
    if (!condition) continue
    const moved = moveCondition(next, condition.atom_id)
    if (moved) {
      next = moved
      applied.push(edit)
    }
  }
  return { ast: next, applied }
}

// Чипы «Я понял так»: атомы одной группы «ИЛИ» — одним чипом, «НЕ» — с «не»; переносится только
// то, что можно перенести, не ломая граф.
export function conditionChips(ast) {
  if (!ast) return []
  const { nodes, parents, root } = index(ast)
  const live = reachable(ast)
  const chips = []
  const grouped = new Set()
  const hardByAtom = new Map((ast.hard || []).map((item) => [item.atom_id, item]))
  if (root) {
    for (const node of nodes.values()) {
      if (node.op !== 'OR' || !live.has(node.node_id)) continue
      const atoms = (node.children || []).map((child) => nodes.get(child)).filter((child) => child?.op === 'ATOM' && hardByAtom.has(child.atom_id))
      if (atoms.length < 2) continue
      const conditions = atoms.map((child) => hardByAtom.get(child.atom_id))
      conditions.forEach((item) => grouped.add(item.atom_id))
      const spans = [...new Set(conditions.map((item) => item.text_span).filter(Boolean))]
      chips.push({ key: `or-${node.node_id}`, kind: 'required', group: 'OR', conditions, label: spans.join(' или '), variants: conditions.length, block: moveBlock(ast, conditions[0].atom_id) })
    }
  }
  for (const item of ast.hard || []) {
    if (grouped.has(item.atom_id)) continue
    const atomNode = [...nodes.values()].find((node) => node.op === 'ATOM' && node.atom_id === item.atom_id)
    const negated = Boolean(atomNode && (parents.get(atomNode.node_id) || []).some((parent) => parent.op === 'NOT'))
    chips.push({ key: `h-${item.atom_id || item.feature_id}`, kind: 'required', group: negated ? 'NOT' : null, conditions: [item], label: negated && item.text_span ? `не ${item.text_span}` : item.text_span, negated, block: moveBlock(ast, item.atom_id) })
  }
  for (const item of ast.soft || []) chips.push({ key: `s-${item.atom_id || item.feature_id}`, kind: 'preferred', group: null, conditions: [item], label: item.text_span, block: moveBlock(ast, item.atom_id) })
  return chips
}

// Проверка, которой пользуется сервер (validate.py): граф ссылается на существующие атомы, веса — 1.
export function astProblems(ast) {
  const out = []
  const hardIds = new Set((ast.hard || []).map((item) => item.atom_id))
  const ooc = new Set((ast.out_of_catalog || []).map((item) => item.atom_id))
  if (ast.logic?.length) {
    const ids = new Set(ast.logic.map((node) => node.node_id))
    for (const node of ast.logic) {
      if (node.op === 'ATOM' && !hardIds.has(node.atom_id) && !ooc.has(node.atom_id)) out.push(`logic ${node.node_id}: нет атома ${node.atom_id}`)
      if (node.op !== 'ATOM' && (!node.children?.length || node.children.some((child) => !ids.has(child)))) out.push(`logic ${node.node_id}: дети не найдены`)
    }
    if (!ids.has(ast.hard_root)) out.push('logic: нет корня')
  }
  if (ast.soft?.length) {
    const total = ast.soft.reduce((sum, item) => sum + item.weight, 0)
    if (Math.abs(total - 1) > 0.01) out.push(`веса в сумме ${total.toFixed(3)}`)
    if (ast.soft.some((item) => !(item.weight > 0 && item.weight <= 1))) out.push('вес вне (0, 1]')
  }
  if ((ast.hard || []).some((item) => item.provenance === 'inferred')) out.push('выведенное обязательное')
  return out
}
