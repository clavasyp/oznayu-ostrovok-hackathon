// Кабинет отельера: подтверждение сведений со страницы Островка и то, что понял агент-отельер.
import { valueWord } from './facts.js'
import { plural } from './trip.js'

// «Верно» — подтверждение предзаполнения: только сами цитаты со страницы, без даты страницы,
// иначе агент примет год за число (так было с «2026» у питомцев).
export function confirmText(prefill) {
  if (!prefill) return ''
  const quotes = /указано:\s*(.+?)(?:\s*\(страница от [^)]*\))?\.\s*Это верно\?\s*$/u.exec(prefill)?.[1]
  return quotes ? `Да, верно: ${quotes}` : 'Да, верно.'
}

export function fieldLabel(fields, name) {
  const label = (fields || []).find((field) => field.name === name)?.label
  return label ? label.split(',')[0] : name
}

export function answerValue(value) {
  if (value == null) return '—'
  if (typeof value === 'object') return (Array.isArray(value) ? value : Object.values(value)).map((item) => answerValue(item)).join(', ')
  return valueWord(value)
}

// Поля, которые понял агент: подпись из анкеты, значение словами и цитата из ответа.
export function agentFields(report, fields) {
  return Object.entries(report?.fields || {}).map(([name, item]) => ({
    name,
    label: fieldLabel(fields, name),
    value: answerValue(item?.value),
    quote: item?.quote || '',
  }))
}

export const ANSWER_STATES = {
  not_asked: 'Не задан',
  asked: 'Отправлен отелю',
  answered: 'Ответ учтён',
  answered_unknown: 'Отель не знает',
  answered_unparsed: 'Ответ не распознан',
}

// --- Где окна номеров: размещения категорий со слов отеля (D17) ---

export const SIDE_WORDS = {
  N: 'север', NE: 'северо-восток', E: 'восток', SE: 'юго-восток', S: 'юг',
  SW: 'юго-запад', W: 'запад', NW: 'северо-запад', courtyard: 'двор',
}

export const GUARANTEE_WORDS = {
  all_guaranteed: 'гарантируют при бронировании',
  exact_room: 'номер выбран точно',
  some_possible: 'конкретный вариант не гарантирован',
  on_request: 'конкретный вариант — по запросу',
}

const PLACEMENT_SETS = { hotelier: 'со слов отеля', simulated_hotelier: 'ответ имитатора', curated: 'проверено командой' }

// Подпись корпуса из API: «адрес — 9 эт. (на карте: «…») [way/1]» → без технического ID в конце.
export function buildingLabel(label) {
  return (label || '').replace(/\s*\[[^\]]+\]\s*$/, '')
}

// Стороны, на которые у корпуса по карте есть стены (от 3 м): по ним отель и выбирает сторону.
const SIDE_ORDER = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW', 'courtyard']
export function wallSides(choice) {
  const sides = new Set([...(choice?.unambiguous_facades || []), ...(choice?.ambiguous_sides || [])].map((item) => item.side))
  return SIDE_ORDER.filter((side) => sides.has(side)).map((side) => SIDE_WORDS[side]).join(', ')
}

// Технические ID корпусов в тексте уточнения: отель различает корпуса по списку и ссылке на карту.
export const withoutBuildingIds = (text) => (text || '').replace(/\s*\[(?:way|relation|node)\/\d+\]/g, '')

// Корпуса по карте — объекты OpenStreetMap: у корпусов с одним адресом отличить их можно только
// на карте. Не похоже на ID OSM — ссылки нет.
export function osmUrl(buildingId) {
  return /^(way|relation|node)\/\d+$/.test(buildingId || '') ? `https://www.openstreetmap.org/${buildingId}` : ''
}

// Первый абзац вопроса: список корпусов под ним показан выбором, а не текстом.
export function placementQuestionText(question) {
  return (question?.question || '').split('\n\n')[0]
}

// Статус вопроса в списке: ответ на этот вопрос или размещения из другого источника.
export function placementState(question) {
  const set = PLACEMENT_SETS[question?.placement_status]
  if (question?.answer_status === 'answered') {
    return { answered: true, text: question.placement_status === 'simulated_hotelier' ? 'Ответ имитатора учтён' : 'Ответ учтён' }
  }
  if (set) return { answered: false, text: `Размещения есть: ${set}` }
  return { answered: false, text: 'Нет ответа' }
}

// Отмеченные корпуса из ответа имитатора: «как называет отель» → building_id.
export function marksFromAliases(aliases) {
  return Object.fromEntries(Object.entries(aliases || {}).map(([name, buildingId]) => [buildingId, { checked: true, name }]))
}

// Тело POST …/placement-answers. Песочница кабинета — всегда ответ имитатора (D41-8).
// Отмеченный корпус без названия не отправляем: API вернёт 422, а отель должен назвать корпус.
export function placementBody(question, text, marks = {}, respondent = 'Песочница') {
  const answer = (text || '').trim()
  if (!answer) return { error: 'Напишите, где окна номеров этой категории' }
  const choices = new Set((question?.building_choices || []).map((item) => item.building_id))
  const aliases = {}
  for (const [buildingId, mark] of Object.entries(marks || {})) {
    if (!mark?.checked || !choices.has(buildingId)) continue
    const name = (mark.name || '').trim()
    if (!name) return { error: 'Напишите, как вы называете отмеченный корпус' }
    if (aliases[name] && aliases[name] !== buildingId) return { error: `Название «${name}» дано двум корпусам` }
    aliases[name] = buildingId
  }
  const body = { room_type_id: question.room_type_id, answer_text: answer, is_simulated: true, respondent }
  if (Object.keys(aliases).length) body.building_aliases = aliases
  return { body }
}

function floorsText(floors) {
  if (!floors) return 'все этажи'
  const [from, to] = floors
  return from === to ? `${from} этаж` : `этажи ${from}–${to}`
}

function sidesText(variant) {
  const sides = (variant.sides || []).map((side) => side === 'courtyard' ? 'во двор' : `на ${SIDE_WORDS[side] || side}`)
  const walls = Object.values(variant.segments || {}).reduce((sum, items) => sum + items.length, 0)
  const wallText = walls > 1 ? ` (${walls} ${plural(walls, 'стена', 'стены', 'стен')}${variant.corner ? '' : ', номер — у одной из них'})` : ''
  return sides.length ? `окна ${sides.join(' и ')}${variant.corner ? ' — в каждом номере' : ''}${wallText}` : 'сторона не названа'
}

// Варианты, которые понял агент: корпус, этажи, стороны (с числом стен по карте).
export function placementVariants(report, choices = []) {
  const labels = new Map((choices || []).map((item) => [item.building_id, buildingLabel(item.label)]))
  return (report?.placements || []).map((variant) => ({
    building: variant.building || labels.get(variant.building_id) || (variant.building_id ? 'корпус по карте' : 'корпус не назван'),
    floors: floorsText(variant.floors),
    sides: sidesText(variant),
    quote: variant.quote || '',
  }))
}

// Итог ответа о размещении для карточки: заголовок, что сохранено, уточнение.
export function placementOutcome(data, question) {
  const variants = placementVariants(data?.agent, question?.building_choices)
  const rejected = (data?.agent?.rejected || []).map((item) => typeof item === 'string' ? item : [item.quote && `«${item.quote}»`, item.reason].filter(Boolean).join(' — '))
  const notes = (data?.notes || []).filter((note) => !/имитатор|без догадок о корпусе/i.test(note))
  const base = { state: data?.state, variants, rejected, notes, followup: withoutBuildingIds(data?.followup), parsedBy: data?.agent?.parsed_by || '' }
  if (data?.state === 'answered') {
    const items = data.placements?.items || []
    const floors = new Set(items.map((item) => `${item.building_id}|${item.floor}`)).size
    const walls = new Set(items.flatMap((item) => (item.windows || []).map((wall) => `${item.building_id}|${wall}`))).size
    const lines = [`Сохранено ${items.length} ${plural(items.length, 'размещение', 'размещения', 'размещений')}: ${floors} ${plural(floors, 'этаж', 'этажа', 'этажей')}, ${walls} ${plural(walls, 'стена', 'стены', 'стен')} по карте`]
    const completeness = data.placements?.completeness
    if (completeness === 'complete') lines.push('Других вариантов нет — список полный')
    else lines.push('Список может быть неполным: гость увидит «Нужно уточнить». Если других вариантов нет — так и напишите')
    const guarantee = GUARANTEE_WORDS[data.placements?.guarantee]
    if (guarantee) lines.push(`Гарантия: ${guarantee}`)
    return { ...base, tone: 'ok', title: 'Размещения сохранены', lines }
  }
  if (data?.state === 'needs_clarification') return { ...base, tone: 'pending', title: 'Нужно уточнение — размещения не сохранены', lines: [] }
  if (data?.state === 'real_answer_kept') return { ...base, tone: 'pending', title: 'Ответ имитатора не сохранён: у категории остаётся настоящий ответ', lines: ['Размещения со слов отеля или проверенные командой ответ из песочницы не заменяет'] }
  return { ...base, tone: 'pending', title: 'Не к чему привязать: корпусов отеля нет в справочнике', lines: [] }
}

// Вопрос после ответа: состояние как в GET …/placement-questions.
export function questionAfter(question, data) {
  const set = data?.room_type?.placement_set
  if (!set) return question
  const answered = set.source?.question_id === question.question_id
  return {
    ...question,
    answer_status: answered ? 'answered' : question.answer_status,
    answered_at: answered ? set.source?.answered_at || question.answered_at : question.answered_at,
    placement_status: set.status ?? null,
    placement_completeness: set.completeness ?? null,
  }
}

// Отель и категория в подборке гостя — после пересчёта; нет на странице — null.
export function searchCheck(results, hotelId, roomTypeId) {
  const result = (results || []).find((item) => item?.hotel?.hotel_id === hotelId)
  if (!result) return null
  const category = (result.categories || []).find((item) => item.room_type_id === roomTypeId)
  return {
    status: result.status,
    statusLabel: result.status_label,
    category: category ? { name: category.name, status: category.status, statusLabel: category.status_label, reason: category.reason || '' } : null,
  }
}
