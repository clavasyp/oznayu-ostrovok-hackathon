// Уточнения разбора (`ast.clarify`) и постраничная выдача. Ответ на уточнение уходит в поиск
// через текст запроса: его разбирают правила или модель, так что смысл решает сервер, а гость
// видит в поле запроса то, что ищем.

const QUIET = /тих|тиш|потише/iu

export function clarifyKind(item) {
  const options = item?.options || []
  if (!options.length) return /дат/iu.test(`${item?.about || ''} ${item?.question || ''}`) ? 'dates' : 'open'
  if (QUIET.test(item.about || '') && options.some((option) => /номер/iu.test(option))) return 'quiet'
  if (options.includes('неважно')) return 'negated'
  if (options.length === 2 && options[0] === 'да' && options[1] === 'нет') return 'confirm'
  return 'choice'
}

export function clarifyKey(item) {
  return `${item?.about || ''}|${item?.question || ''}`
}

function insertAfter(text, fragment, addition) {
  const at = text.toLowerCase().indexOf((fragment || '').toLowerCase())
  if (!fragment || at < 0) return null
  const end = at + fragment.length
  return `${text.slice(0, end)}${addition}${text.slice(end)}`
}

function asSentence(text, phrase) {
  const base = text.trim().replace(/[\s,;:]+$/u, '')
  const tail = phrase.charAt(0).toUpperCase() + phrase.slice(1)
  return `${/[.!?]$/u.test(base) ? base : `${base}.`} ${tail}.`
}

function questionSubject(item) {
  return /«([^»]+)»/u.exec(item?.question || '')?.[1] || item?.about || ''
}

// Новый текст запроса по выбору гостя; `text: null` — текст не меняется, уточнение просто снято.
export function refineQuery(text, item, option) {
  const kind = clarifyKind(item)
  if (kind === 'quiet') {
    // «Тихий (в номере)» — тишина в номере; «тихий (снаружи)» — подальше от дорог и баров;
    // оба — отдельной фразой, чтобы не сменить обязательность соседних условий.
    const both = /друг|оба|и то/iu.test(option)
    const indoor = both || /номер/iu.test(option)
    const inserted = insertAfter(text, item.about, indoor ? ' (в номере)' : ' (снаружи)')
    if (!inserted) return { text: asSentence(text, indoor ? 'тишина в номере' : 'тихо снаружи'), note: '' }
    return { text: both ? asSentence(inserted, 'снаружи тоже тихо') : inserted, note: '' }
  }
  if (kind === 'confirm') {
    if (option === 'да') return { text: asSentence(text, questionSubject(item)), note: '' }
    return { text: null, note: 'Не учитываем — выдача не меняется' }
  }
  if (kind === 'negated') {
    if (option === 'неважно') return { text: null, note: 'Не учитываем — выдача не меняется' }
    return { text: asSentence(text, `не ${questionSubject(item)}`), note: '' }
  }
  if (kind === 'choice') return { text: asSentence(text, `${item.about} — ${option}`), note: '' }
  return { text: null, note: '' }
}

// --- Постраничная выдача: `limit` / `offset` в POST /api/search, `total` и `counts` в ответе.
// Без них (старый API) все результаты приходят сразу и показываются частями на месте.

export const PAGE_SIZE = 30
export const STATUSES = ['fits', 'needs_check', 'not_fit']

export function serverPaged(data) {
  return Number.isInteger(data?.total)
}

export function searchCounts(data) {
  const counts = { fits: 0, needs_check: 0, not_fit: 0 }
  if (data?.counts && typeof data.counts === 'object') {
    for (const status of STATUSES) counts[status] = Number(data.counts[status]) || 0
    return counts
  }
  for (const result of data?.results || []) if (result.status in counts) counts[result.status] += 1
  return counts
}

export function searchTotal(data) {
  return serverPaged(data) ? data.total : (data?.results || []).length
}

// Выдача отсортирована по статусу (core/search.py): «Подходит», «Нужно уточнить», «Не подходит».
export function statusStart(status, counts) {
  let start = 0
  for (const item of STATUSES) {
    if (item === status) return start
    start += counts[item] || 0
  }
  return 0
}

export function filterTotal(status, data) {
  return status === 'all' ? searchTotal(data) : searchCounts(data)[status] || 0
}

// Окно выдачи для фильтра: с какого места просить у сервера и сколько всего.
export function pageWindow(status, data) {
  const counts = searchCounts(data)
  if (status === 'all') return { start: 0, total: searchTotal(data) }
  return { start: statusStart(status, counts), total: counts[status] || 0 }
}
