// «Откуда знаем»: факты с паспортом словами, без JSON. Факты окон (их тысячи у отеля) —
// одной строкой на характеристику, подробности — в объяснении.
import { MONTHS_NOM, formatDay, plural } from './trip.js'

export const WINDOW_LEVELS = new Set(['facade_physical_floor', 'facade', 'physical_floor', 'placement'])

const SUBJECT_LEVELS = {
  hotel: 'отель', building: 'корпус', building_part: 'часть корпуса', facade: 'фасад',
  physical_floor: 'этаж', facade_physical_floor: 'окно (фасад и этаж)', room_type: 'категория номера',
  placement: 'размещение', route: 'маршрут',
}
const CONFIDENCE = { high: 'высокая', medium: 'средняя', low: 'низкая', unknown: 'неизвестна' }
const ATTEMPT_STATES = { accepted: 'принят', rejected: 'отклонён', skipped: 'пропущен', failed: 'ошибка' }
const TARGETS = {
  beach: 'пляж', stop: 'остановка', center: 'центр', airport_terminal: 'аэропорт', railway_station: 'вокзал',
  designated_dog_area: 'площадка для собак', pharmacy: 'аптека', playground: 'детская площадка', sight: 'достопримечательность',
}
const PROVIDERS = { hotelier: 'Ответ отеля', hotelier_simulator: 'Имитатор отельера', ostrovok: 'Страница Островка' }
const WORDS = { present: 'есть', none: 'нет', unknown: 'неизвестно', split: 'сплит-система', central: 'центральный', dog: 'собаки', cat: 'кошки', dog_cat: 'собаки и кошки', any: 'любые животные', own: 'своя', third_party: 'сторонняя', full: 'полностью', partial: 'частично', double: 'двуспальная кровать', twin: 'две кровати', type2: 'Type 2', '24/7': 'круглосуточно' }
const WHY = { clear: 'ничто не закрывает', blocked: 'закрыто зданием', terrain: 'закрыто рельефом', trees: 'закрыто деревьями', behind_facade: 'солнце за фасадом', no_sea: 'моря нет в секторе' }

export const formatNumber = (value) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value)

export function formatDateTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }).format(date)
}

export const subjectLevelText = (level) => SUBJECT_LEVELS[level] || level || ''
export const confidenceText = (level) => CONFIDENCE[level] || level || ''
export const attemptStateText = (state) => ATTEMPT_STATES[state] || state
export const providerText = (provider) => PROVIDERS[provider] || (provider || '').replaceAll('_', ' ')

const SURFACE = { sand: 'песок', pebble: 'галька', rock: 'скалы', mixed: 'смешанное' }
export const valueWord = (value) => typeof value === 'boolean' ? (value ? 'да' : 'нет') : typeof value === 'number' ? formatNumber(value) : WORDS[value] || String(value)
const word = valueWord

// Понятные параметры факта; служебные (хеши, сетка, протоколы, пути) не показываем.
export function paramWords(params) {
  const out = []
  if (!params || typeof params !== 'object') return out
  const month = Number(params.month)
  if (Number.isInteger(month) && month >= 1 && month <= 12) out.push(['Месяц', MONTHS_NOM[month - 1]])
  if (typeof params.date === 'string') out.push(['Дата', formatDay(params.date)])
  if (params.target_name) out.push(['Цель', String(params.target_name)])
  else if (params.target_kind) out.push(['Цель', TARGETS[params.target_kind] || String(params.target_kind).replaceAll('_', ' ')])
  if (Number.isFinite(params.radius_m)) out.push(['Радиус', `${formatNumber(params.radius_m)} м`])
  if (params.arrival_local) out.push(['Приезд', formatDateTime(params.arrival_local)])
  if (params.windows_state) out.push(['Окна', params.windows_state === 'open' ? 'открыты' : 'закрыты'])
  return out
}

// Значение факта одной строкой; неизвестный объект — «подробности в паспорте», а не JSON.
const UNITS = { m: 'м', count: '', sections: '', 'days/month': 'дн.', h: 'ч', '°C': '°C', '%': '%' }

function unitText(unit) {
  if (!unit) return ''
  const shown = unit in UNITS ? UNITS[unit] : unit.length <= 6 ? unit : ''
  return shown ? ` ${shown}` : ''
}

export function formatFactValue(fact, fieldLabels = {}) {
  if (fact?.value == null) return fact?.unknown_reason_text || fact?.unknown_reason || 'Неизвестно'
  const value = fact.value
  const unit = unitText(fact.unit)
  if (typeof value !== 'object') return typeof value === 'boolean' ? (value ? 'Да' : 'Нет') : `${typeof value === 'number' ? formatNumber(value) : WORDS[value] || value}${typeof value === 'number' ? unit : ''}`
  if (Array.isArray(value)) return value.length ? value.slice(0, 3).map((item) => typeof item === 'object' ? item?.name || item?.type || '' : word(item)).filter(Boolean).join(', ') || 'подробности в паспорте' : 'нет'
  const summary = []
  // Новые слои: охранный статус, год постройки, транспорт 2ГИС, часы солнца, горы.
  if (value.heritage === true) return ['объект наследия', value.category, value.name ? `«${value.name}»` : '', value.registry_id ? `реестр № ${value.registry_id}` : ''].filter(Boolean).join(' · ')
  if (Number.isInteger(value.year)) return `${value.year} г.`
  if (value.origin && 'route_exists' in value) {
    if (!value.route_exists) return `маршрута от «${value.origin}» нет`
    const n = value.transfers || 0
    return `${value.duration_min} мин · ${n ? `пересадок: ${n}` : 'без пересадок'} · от «${value.origin}»`
  }
  if (value.bound === 'upper' && Number.isFinite(value.hours)) return `до ${formatNumber(value.hours)} ч прямого солнца`
  if (value.bound === 'lower' && Number.isFinite(value.distance_m)) return `ближе ${value.distance_m >= 1000 ? `${formatNumber(value.distance_m / 1000)} км` : `${formatNumber(value.distance_m)} м`} на карте нет`
  if (value.surface && 'beach_id' in value) return [SURFACE[value.surface] || value.surface, value.beach_name ? `пляж «${value.beach_name}»` : 'ближайший пляж'].join(' · ')
  if (Number.isFinite(value.highest_m)) summary.push(`горы до ${formatNumber(value.highest_m)} м`)
  if (typeof value.visible === 'boolean') summary.push(value.visible ? 'видно' : `не видно${WHY[value.why] ? ` — ${WHY[value.why]}` : ''}`)
  if (Number.isFinite(value.aboveground_levels)) summary.push(`${value.aboveground_levels} этажей`)
  if (Number.isFinite(value.distance_m)) summary.push(`${Math.round(value.distance_m)} м`)
  if (Number.isFinite(value.mean_C)) summary.push(`${formatNumber(value.mean_C)} °C`)
  else if (Number.isFinite(value.mean)) summary.push(`${formatNumber(value.mean)}${unit}`)
  if (Number.isFinite(value.dsm_positive_dz_estimate_m)) summary.push(`подъём ≈ ${formatNumber(value.dsm_positive_dz_estimate_m)} м`)
  if (Number.isFinite(value.known_step_sections)) summary.push(`лестниц: ${value.known_step_sections}${Number.isFinite(value.mapped_steps_length_m) && value.mapped_steps_length_m > 0 ? ` (${formatNumber(value.mapped_steps_length_m)} м)` : ''}`)
  if (Number.isFinite(value.known_crossings)) summary.push(`переходов: ${value.known_crossings}`)
  if (Number.isFinite(value.known_lit_percent)) summary.push(`освещено ${formatNumber(value.known_lit_percent)} %${Number.isFinite(value.unknown_percent) ? `, неизвестно ${formatNumber(value.unknown_percent)} %` : ''}`)
  if (Number.isFinite(value.count)) summary.push(`${value.count} ${plural(value.count, 'объект', 'объекта', 'объектов')}`)
  if (Array.isArray(value.objects) && value.objects.length) summary.push(value.objects.slice(0, 2).map((item) => item.name || item.type).filter(Boolean).join(', '))
  if (value.target_name) summary.push(String(value.target_name))
  if (Array.isArray(value.names) && value.names.length) summary.push(value.names.join(', ') + (value.seasonal ? ' (сезонный)' : ''))
  if (summary.length) return summary.filter(Boolean).join(' · ')
  // Ответ отеля: поля вопроса — подписи из анкеты (если есть), значения словами.
  const fields = Object.entries(value).filter(([, item]) => item != null && typeof item !== 'object')
  if (fields.length) return fields.slice(0, 4).map(([key, item]) => `${fieldLabels[key] ? `${fieldLabels[key].split(',')[0]}: ` : ''}${word(item)}`).join(' · ')
  return 'подробности в паспорте'
}

// Факты отеля для «Откуда знаем»: обычные — по одному, факты окон — одной строкой на характеристику.
export function factRows(factsByFeature, relevantIds = new Set()) {
  const rows = []
  for (const [featureId, facts] of Object.entries(factsByFeature || {})) {
    const windows = (facts || []).filter((fact) => WINDOW_LEVELS.has(fact.subject_level))
    for (const fact of facts || []) if (!WINDOW_LEVELS.has(fact.subject_level)) rows.push({ kind: 'fact', key: fact.fact_id, featureId, fact, relevant: relevantIds.has(fact.fact_id) })
    if (windows.length) {
      const tally = { yes: 0, no: 0, estimate: 0, unknown: 0 }
      for (const fact of windows) {
        const visible = typeof fact.value === 'object' && fact.value ? fact.value.visible : fact.value
        if (fact.status === 'unknown' || fact.value == null) tally.unknown += 1
        else if (fact.status === 'estimate') tally.estimate += 1
        else if (visible === false) tally.no += 1
        else tally.yes += 1
      }
      rows.push({ kind: 'windows', key: `windows-${featureId}`, featureId, count: windows.length, tally, relevant: windows.some((fact) => relevantIds.has(fact.fact_id)) })
    }
  }
  return rows.sort((a, b) => Number(b.relevant) - Number(a.relevant))
}

export function windowsText(row) {
  const parts = []
  if (row.tally.yes) parts.push(`«да» по расчёту — ${row.tally.yes}`)
  if (row.tally.no) parts.push(`«нет» по расчёту — ${row.tally.no}`)
  if (row.tally.estimate) parts.push(`оценка — ${row.tally.estimate}`)
  if (row.tally.unknown) parts.push(`неизвестно — ${row.tally.unknown}`)
  return `${row.count} ${row.count % 10 === 1 && row.count % 100 !== 11 ? 'окно' : [2, 3, 4].includes(row.count % 10) && ![12, 13, 14].includes(row.count % 100) ? 'окна' : 'окон'}${parts.length ? `: ${parts.join(', ')}` : ''}`
}

// Паспорт ответа отеля: цитата, вопрос, время, кто ответил.
export function hotelierSource(fact) {
  const source = (fact?.sources || []).find((item) => item.provider === 'hotelier' || item.provider === 'hotelier_simulator')
  if (!source && fact?.status !== 'hotelier' && fact?.status !== 'simulated_hotelier') return null
  return {
    quote: source?.quote || '',
    questionId: source?.question_id != null ? String(source.question_id) : '',
    answeredAt: formatDateTime(source?.answered_at || fact?.observed_at),
    respondent: source?.respondent || '',
    simulated: fact?.status === 'simulated_hotelier' || source?.provider === 'hotelier_simulator',
  }
}
