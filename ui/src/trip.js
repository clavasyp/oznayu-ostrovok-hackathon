const DAY_MS = 86_400_000

export const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']
export const MONTHS_NOM = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

// Пояс города — «сегодня» по городу поездки, а не по устройству.
export const CITY_TIME_ZONES = {
  moscow: 'Europe/Moscow', sochi: 'Europe/Moscow', spb: 'Europe/Moscow', kazan: 'Europe/Moscow',
  kaliningrad: 'Europe/Kaliningrad', minsk: 'Europe/Minsk', istanbul: 'Europe/Istanbul', dubai: 'Asia/Dubai',
}

export function parseDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return null
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null
}

export function todayIso(now = new Date(), timeZone = undefined) {
  if (timeZone) {
    try {
      const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now).map((part) => [part.type, part.value]))
      if (parts.year && parts.month && parts.day) return `${parts.year}-${parts.month}-${parts.day}`
    } catch { /* неизвестный пояс — по устройству */ }
  }
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

// Пояс — из `/api/cities` (`timezone`, режим базы), иначе из словаря выше.
export function cityToday(cityId, timeZone = undefined, now = new Date()) {
  return todayIso(now, timeZone || CITY_TIME_ZONES[cityId])
}

export function addIsoDays(value, days) {
  const date = parseDate(value)
  if (!date) return ''
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

export function nightCount(checkIn, checkOut) {
  const start = parseDate(checkIn)
  const end = parseDate(checkOut)
  return start && end && end > start ? (end - start) / DAY_MS : 0
}

export function stayError(stay, today = todayIso()) {
  if (!parseDate(stay.checkIn) || !parseDate(stay.checkOut)) return 'Укажите даты заезда и выезда.'
  if (stay.checkIn < today) return 'Дата заезда уже прошла. Выберите новую дату.'
  if (!nightCount(stay.checkIn, stay.checkOut)) return 'Выезд должен быть позже заезда.'
  if (!Number.isInteger(stay.adults) || stay.adults < 1 || stay.adults > 8 || !Number.isInteger(stay.children) || stay.children < 0 || stay.children > 6) return 'Выберите от 1 до 8 взрослых и до 6 детей.'
  return ''
}

export function defaultStay(today = todayIso()) {
  const checkIn = addIsoDays(today, 14)
  return { checkIn, checkOut: addIsoDays(checkIn, 3), adults: 2, children: 0 }
}

export function plural(number, one, few, many) {
  const mod100 = Math.abs(number) % 100
  const mod10 = mod100 % 10
  return mod100 >= 11 && mod100 <= 14 ? many : mod10 === 1 ? one : mod10 >= 2 && mod10 <= 4 ? few : many
}

export function formatNights(count) {
  return `${count} ${plural(count, 'ночь', 'ночи', 'ночей')}`
}

export function formatGuests(adults, children) {
  const adultText = `${adults} ${plural(adults, 'взрослый', 'взрослых', 'взрослых')}`
  return children ? `${adultText} · ${children} ${plural(children, 'ребёнок', 'ребёнка', 'детей')}` : adultText
}

export function formatDateRange(checkIn, checkOut, full = false) {
  const start = parseDate(checkIn)
  const end = parseDate(checkOut)
  if (!start || !end) return 'Выберите даты'
  const months = full ? MONTHS_GEN : MONTHS_SHORT
  const day = (date) => `${date.getUTCDate()} ${months[date.getUTCMonth()]}`
  if (start.getUTCFullYear() !== end.getUTCFullYear()) return `${day(start)} ${start.getUTCFullYear()} — ${day(end)} ${end.getUTCFullYear()}`
  const range = start.getUTCMonth() === end.getUTCMonth() ? `${start.getUTCDate()}–${day(end)}` : `${day(start)} — ${day(end)}`
  return full ? `${range} ${start.getUTCFullYear()}` : range
}

export function formatDay(value) {
  const date = parseDate(value)
  return date ? `${date.getUTCDate()} ${MONTHS_GEN[date.getUTCMonth()]} ${date.getUTCFullYear()}` : value || ''
}

// Даты поездки словами, как их понимает разбор запроса (agents/search/rules.py): «10–13 ноября 2026».
export function tripDatesText(checkIn, checkOut) {
  const start = parseDate(checkIn)
  const end = parseDate(checkOut)
  if (!start || !end || end <= start) return ''
  const sameMonth = start.getUTCFullYear() === end.getUTCFullYear() && start.getUTCMonth() === end.getUTCMonth()
  if (sameMonth) return `${start.getUTCDate()}–${end.getUTCDate()} ${MONTHS_GEN[end.getUTCMonth()]} ${end.getUTCFullYear()}`
  return `${formatDay(checkIn)} — ${formatDay(checkOut)}`
}

const MONTH_WORD = '(?:январ|феврал|март|апрел|ма[йея]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-яё]*'
const ONE_DATE = `\\d{1,2}(?:\\s*[–-]\\s*\\d{1,2})?\\s+${MONTH_WORD}(?:\\s+\\d{4}(?:\\s*(?:года|г\\.?|-го))?)?`
// «10–13 ноября», «с 10 по 13 ноября», «с 10 ноября по 3 декабря», «30 декабря 2026 — 3 января 2027».
const DATE_EXPR = new RegExp(`(?:(?<![а-яё])с\\s+(?:\\d{1,2}\\s+(?:по|до)\\s+)?)?${ONE_DATE}(?:\\s*(?:[—–-]|по|до)\\s*${ONE_DATE})?`, 'iu')

export function hasTripDates(text) {
  return DATE_EXPR.test(text || '')
}

// Выбранные даты уходят в поиск через текст запроса: разбор ставит их в условия (закат на дату,
// месяц у температуры моря) и снимает уточнение «на какие даты». Даты в тексте заменяются.
export function withTripDates(query, stay) {
  const dates = tripDatesText(stay?.checkIn, stay?.checkOut)
  const text = (query || '').trim()
  if (!dates || !text) return text
  if (DATE_EXPR.test(text)) return text.replace(DATE_EXPR, dates)
  return `${text.replace(/[\s,.;:]+$/u, '')}, ${dates}`
}

// Даты и гости из разобранного запроса — в поездку.
export function stayFromAst(ast, stay, today = todayIso()) {
  let next = stay
  const from = ast?.dates?.from
  const to = ast?.dates?.to
  if (from && parseDate(from)) {
    const checkOut = to && parseDate(to) && to > from ? to : from === stay.checkIn && stay.checkOut > from ? stay.checkOut : addIsoDays(from, 1)
    const candidate = { ...next, checkIn: from, checkOut }
    if (!stayError(candidate, today)) next = candidate
  }
  const adults = ast?.guests?.adults
  if (Number.isInteger(adults) && adults >= 1) next = { ...next, adults: Math.min(8, adults) }
  const children = ast?.guests?.children_ages?.length
  if (children) next = { ...next, children: Math.min(6, children) }
  return next.checkIn === stay.checkIn && next.checkOut === stay.checkOut && next.adults === stay.adults && next.children === stay.children ? stay : next
}

// Месяц, на который посчитано условие: `params.month` (температура моря, солнце) или `params.date` (закат).
export function conditionMonth(params) {
  const month = Number(params?.month)
  if (Number.isInteger(month) && month >= 1 && month <= 12) return month
  const date = parseDate(params?.date)
  return date ? date.getUTCMonth() + 1 : null
}

export function conditionPeriod(params) {
  if (parseDate(params?.date)) return formatDay(params.date).replace(/ \d{4}$/, '')
  const month = conditionMonth(params)
  return month ? MONTHS_NOM[month - 1] : ''
}

// Период факта в паспорте: дата или месяц условия, а у маршрутов на даты поездки (W01 —
// сезонный доступ к пляжу) — месяц начала поездки: «пешком до пляжа · ноябрь».
export function factPeriod(params) {
  const own = conditionPeriod(params)
  if (own) return own
  const start = parseDate(params?.trip_start)
  return start ? MONTHS_NOM[start.getUTCMonth()] : ''
}

function stayMonths(stay) {
  const months = new Set()
  const nights = nightCount(stay.checkIn, stay.checkOut)
  for (let i = 0; i < Math.max(nights, 1); i += 1) {
    const date = parseDate(addIsoDays(stay.checkIn, i))
    if (date) months.add(date.getUTCMonth() + 1)
  }
  return months
}

// Условие посчитано на другой месяц или дату, чем поездка, — не писать «выполнено».
export function periodMismatch(params, stay) {
  const month = conditionMonth(params)
  if (!month || !parseDate(stay?.checkIn) || !parseDate(stay?.checkOut)) return ''
  const trip = formatDateRange(stay.checkIn, stay.checkOut, true)
  if (parseDate(params?.date)) {
    const inside = params.date >= stay.checkIn && params.date < stay.checkOut
    return inside ? '' : `Посчитано на ${formatDay(params.date)}, а поездка — ${trip}`
  }
  return stayMonths(stay).has(month) ? '' : `Посчитано для ${MONTHS_GEN[month - 1]}, а поездка — ${trip}`
}
