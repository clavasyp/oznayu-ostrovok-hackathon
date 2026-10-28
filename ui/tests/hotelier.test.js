import test from 'node:test'
import assert from 'node:assert/strict'
import { agentFields, buildingLabel, confirmText, marksFromAliases, osmUrl, placementBody, placementOutcome, placementQuestionText, placementState, questionAfter, searchCheck, wallSides, withoutBuildingIds } from '../src/hotelier.js'

test('«Верно» confirms only the quotes from the Ostrovok page, without the page date', () => {
  const prefill = 'На странице отеля на Островке указано: «Размещение с домашними животными», «Возможно размещение с животными весом до 8 кг» (страница от 2026-10-23). Это верно?'
  assert.equal(confirmText(prefill), 'Да, верно: «Размещение с домашними животными», «Возможно размещение с животными весом до 8 кг»')
  assert.doesNotMatch(confirmText(prefill), /2026/)
  assert.equal(confirmText(null), '')
})

test('what the agent understood is shown with field labels, words and quotes', () => {
  const report = { fields: { species: { value: 'dog_cat', quote: 'Можно с собаками и кошками' }, prior_approval: { value: true, quote: 'нужно заранее согласовать' } } }
  const fields = [{ name: 'species', label: 'какие животные' }, { name: 'prior_approval', label: 'согласовать заранее, да/нет' }]
  assert.deepEqual(agentFields(report, fields), [
    { name: 'species', label: 'какие животные', value: 'собаки и кошки', quote: 'Можно с собаками и кошками' },
    { name: 'prior_approval', label: 'согласовать заранее', value: 'да', quote: 'нужно заранее согласовать' },
  ])
  assert.deepEqual(agentFields(null, fields), [])
})

const placementQuestion = {
  question_id: 'qp_c688182827ceeed7',
  hotel_id: '7936791',
  room_type_id: '7936791:3202302',
  question: 'Категория «Двухместный номер Стандартный» (7936791:3202302): на какую сторону выходят окна и с каких этажей?\n\nКорпуса отеля по карте — отметьте корпус в списке и напишите, как вы его называете:\n1) …',
  building_choices: [
    { building_id: 'way/1', label: 'улица Орджоникидзе, 17 — 27 эт. (на карте: «Hyatt Regency Sochi») [way/1]' },
    { building_id: 'way/2', label: 'улица Орджоникидзе, 17 — 8 эт. [way/2]' },
  ],
}

test('placement answer from the sandbox is simulated and carries only marked, named buildings', () => {
  const marks = { 'way/2': { checked: true, name: ' корпус 2 ' }, 'way/1': { checked: false, name: 'главный' }, 'way/9': { checked: true, name: 'чужой' } }
  assert.deepEqual(placementBody(placementQuestion, '  Окна на юго-запад, с 9 по 15 этаж (корпус 2).  ', marks).body, {
    room_type_id: '7936791:3202302',
    answer_text: 'Окна на юго-запад, с 9 по 15 этаж (корпус 2).',
    is_simulated: true,
    respondent: 'Песочница',
    building_aliases: { 'корпус 2': 'way/2' },
  })
  // Корпус не отмечен — поля нет: единственный корпус отеля API подставит сам.
  assert.equal('building_aliases' in placementBody(placementQuestion, 'Окна на юг, 3 этаж').body, false)
})

test('placement answer is not sent without text, with an unnamed marked building or one name for two buildings', () => {
  assert.match(placementBody(placementQuestion, '   ').error, /Напишите/)
  assert.match(placementBody(placementQuestion, 'Окна на юг', { 'way/1': { checked: true, name: '  ' } }).error, /называете/)
  const twice = { 'way/1': { checked: true, name: 'корпус А' }, 'way/2': { checked: true, name: 'корпус А' } }
  assert.match(placementBody(placementQuestion, 'Окна на юг', twice).error, /двум корпусам/)
})

test('simulator building aliases become marks and go back unchanged', () => {
  const aliases = { 'корпус 2': 'way/2' }
  const marks = marksFromAliases(aliases)
  assert.deepEqual(marks, { 'way/2': { checked: true, name: 'корпус 2' } })
  assert.deepEqual(placementBody(placementQuestion, 'Окна на юг', marks).body.building_aliases, aliases)
  assert.deepEqual(marksFromAliases(undefined), {})
})

test('building labels and the question are shown without technical IDs and the list of buildings', () => {
  assert.equal(buildingLabel(placementQuestion.building_choices[0].label), 'улица Орджоникидзе, 17 — 27 эт. (на карте: «Hyatt Regency Sochi»)')
  assert.equal(placementQuestionText(placementQuestion), 'Категория «Двухместный номер Стандартный» (7936791:3202302): на какую сторону выходят окна и с каких этажей?')
  // Корпуса с одним адресом различимы только на карте.
  assert.equal(osmUrl('way/260633950'), 'https://www.openstreetmap.org/way/260633950')
  assert.equal(osmUrl('relation/9273776'), 'https://www.openstreetmap.org/relation/9273776')
  assert.equal(osmUrl('b-1'), '')
  assert.equal(osmUrl(undefined), '')
})

test('answered placement: how many placements, floors and walls, completeness and what the agent understood', () => {
  const items = [9, 10].flatMap((floor) => ['seg-a', 'seg-b'].map((wall) => ({ building_id: 'way/1', floor, windows: [wall], room_variant: wall })))
  const data = {
    state: 'answered',
    followup: null,
    placements: { completeness: 'complete', guarantee: 'some_possible', items },
    notes: ['Ответ имитатора: размещения и названия корпусов помечены simulated_hotelier.'],
    agent: { parsed_by: 'rules', rejected: [], placements: [{ building: '', building_id: 'way/1', sides: ['SW'], corner: false, floors: [9, 10], quote: 'окна на юго-запад, с 9 по 10 этаж', segments: { SW: ['seg-a', 'seg-b'] } }] },
  }
  const outcome = placementOutcome(data, placementQuestion)
  assert.equal(outcome.tone, 'ok')
  assert.deepEqual(outcome.lines, ['Сохранено 4 размещения: 2 этажа, 2 стены по карте', 'Других вариантов нет — список полный', 'Гарантия: конкретный вариант не гарантирован'])
  assert.deepEqual(outcome.variants, [{ building: 'улица Орджоникидзе, 17 — 27 эт. (на карте: «Hyatt Regency Sochi»)', floors: 'этажи 9–10', sides: 'окна на юго-запад (2 стены, номер — у одной из них)', quote: 'окна на юго-запад, с 9 по 10 этаж' }])
  assert.deepEqual(outcome.notes, [])
  assert.equal(outcome.parsedBy, 'rules')
  const partial = placementOutcome({ ...data, placements: { ...data.placements, completeness: 'unknown', guarantee: 'all_guaranteed' } }, placementQuestion)
  assert.match(partial.lines[1], /Нужно уточнить/)
  assert.equal(partial.lines[2], 'Гарантия: гарантируют при бронировании')
})

test('clarification, kept real answer and nothing to bind are told apart', () => {
  const clarify = placementOutcome({
    state: 'needs_clarification',
    followup: 'У корпуса улица Ленина, 219/28 — 15 эт. [way/53742331] по карте нет стены на юго-запад (от 3 м) — уточните сторону.',
    notes: ['Размещение не сохранено до уточнения: без догадок о корпусе.', 'Ответ имитатора: размещения и названия корпусов помечены simulated_hotelier.'],
    agent: { parsed_by: 'rules', rejected: [{ field: 'building', reason: 'здания way/9 нет среди корпусов отеля' }], placements: [{ building: 'корпус 9', building_id: null, sides: ['courtyard'], corner: false, floors: [3, 3], quote: 'во двор, 3 этаж' }] },
  }, placementQuestion)
  assert.equal(clarify.tone, 'pending')
  assert.match(clarify.title, /не сохранены/)
  assert.equal(clarify.followup, 'У корпуса улица Ленина, 219/28 — 15 эт. по карте нет стены на юго-запад (от 3 м) — уточните сторону.')
  assert.deepEqual(clarify.rejected, ['здания way/9 нет среди корпусов отеля'])
  assert.deepEqual(clarify.notes, [])
  assert.deepEqual(clarify.variants, [{ building: 'корпус 9', floors: '3 этаж', sides: 'окна во двор', quote: 'во двор, 3 этаж' }])
  assert.match(placementOutcome({ state: 'real_answer_kept', notes: [] }, placementQuestion).title, /настоящий ответ/)
  assert.match(placementOutcome({ state: 'not_placed', notes: [] }, placementQuestion).title, /корпусов отеля нет/)
})

test('question state follows the saved placement set of the category', () => {
  assert.deepEqual(placementState({ answer_status: 'not_answered', placement_status: null }), { answered: false, text: 'Нет ответа' })
  assert.deepEqual(placementState({ answer_status: 'not_answered', placement_status: 'curated' }), { answered: false, text: 'Размещения есть: проверено командой' })
  const set = { status: 'simulated_hotelier', completeness: 'complete', source: { question_id: placementQuestion.question_id, answered_at: '2026-10-28T08:00:00+00:00' } }
  const after = questionAfter({ ...placementQuestion, answer_status: 'not_answered' }, { room_type: { placement_set: set } })
  assert.equal(after.answer_status, 'answered')
  assert.equal(after.placement_completeness, 'complete')
  assert.deepEqual(placementState(after), { answered: true, text: 'Ответ имитатора учтён' })
  // Набор с другого вопроса (или его нет) — ответ на этот вопрос не засчитываем.
  assert.equal(questionAfter({ ...placementQuestion, answer_status: 'not_answered' }, { room_type: { placement_set: { ...set, source: { question_id: 'qp_other' } } } }).answer_status, 'not_answered')
  assert.equal(questionAfter(placementQuestion, { room_type: {} }), placementQuestion)
})

test('guest search check finds the hotel and the answered category on the page', () => {
  const results = [{ hotel: { hotel_id: '7936791' }, status: 'fits', status_label: 'Подходит', categories: [{ room_type_id: '7936791:3202302', name: 'Двухместный номер Стандартный', status: 'fits', status_label: 'Подходит' }] }]
  assert.deepEqual(searchCheck(results, '7936791', '7936791:3202302'), { status: 'fits', statusLabel: 'Подходит', category: { name: 'Двухместный номер Стандартный', status: 'fits', statusLabel: 'Подходит', reason: '' } })
  assert.equal(searchCheck(results, '7936791', 'other').category, null)
  assert.equal(searchCheck(results, '1', '1:2'), null)
})

test('walls by the map are listed in compass order, and building IDs are dropped from clarifications', () => {
  const choice = { unambiguous_facades: [{ side: 'E', facade_id: 'seg-1' }], ambiguous_sides: [{ side: 'W', facade_ids: ['seg-2', 'seg-3'] }, { side: 'N', facade_ids: ['seg-4', 'seg-5'] }, { side: 'S', facade_ids: ['seg-6'] }] }
  assert.equal(wallSides(choice), 'север, восток, юг, запад')
  assert.equal(wallSides(undefined), '')
  assert.equal(withoutBuildingIds('Какой это корпус?\n1) улица 65 лет Победы, 50 — 9 эт. [way/207598815]\n2) там же — 8 эт. [relation/1]'), 'Какой это корпус?\n1) улица 65 лет Победы, 50 — 9 эт.\n2) там же — 8 эт.')
})
