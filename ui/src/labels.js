// Подписи на экране — из API (`label`, `feature_name` у пунктов, `/api/meta/labels.features`),
// а не из своего словаря кодов. Пометки статусов фактов — D18 (оценка) и D31 (имитатор).

export const STATUS_LABELS = { fits: 'Подходит', needs_check: 'Нужно уточнить', not_fit: 'Не подходит' }

// Как читать статус факта рядом с вердиктом: оценка не закрывает обязательное (D18),
// ответ имитатора — только для показа (D31).
export const STATUS_MARKS = {
  estimate: 'оценка',
  simulated_hotelier: 'ответ имитатора отельера',
  hotelier: 'со слов отеля',
  ostrovok_content: 'данные Островка',
  curated: 'проверено командой',
}
const MARK_ORDER = ['estimate', 'simulated_hotelier', 'hotelier', 'ostrovok_content', 'curated']

export const PROVENANCE = {
  observed: 'Источник данных', computed: 'Посчитано', estimate: 'Оценка',
  ostrovok_content: 'Данные Островка', curated: 'Проверено командой',
  hotelier: 'Со слов отеля', simulated_hotelier: 'Ответ имитатора отельера',
  unknown: 'Неизвестно', not_applicable: 'Не применяется',
}

const HOTEL_KINDS = {
  HOTEL: 'Отель', RESORT: 'Курорт', GUESTHOUSE: 'Гостевой дом', APARTMENT: 'Апартаменты',
  APARTHOTEL: 'Апарт-отель', APART_HOTEL: 'Апарт-отель', HOSTEL: 'Хостел', MINI_HOTEL: 'Мини-отель',
  BOUTIQUE_HOTEL: 'Бутик-отель', SANATORIUM: 'Санаторий', VILLA: 'Вилла', COTTAGE: 'Коттедж',
  GLAMPING: 'Глэмпинг', CAMPING: 'Кемпинг', BNB: 'Мини-гостиница', LIVING_QUARTERS: 'Жильё для гостей',
  CASTLE: 'Замок', FARM: 'Гостевая ферма', CAPSULE_HOTEL: 'Капсульный отель',
}

export function hotelKind(kind) {
  if (!kind) return 'Отель'
  return HOTEL_KINDS[kind] || 'Жильё'
}

export function featureName(id, labels) {
  return (id && labels?.features?.[id]) || id || ''
}

// Строка пункта проверки: слова гостя (`label`), затем фрагмент, затем название характеристики.
export function itemTitle(item, labels) {
  return item?.label || item?.text_span || item?.feature_name || featureName(item?.feature_id, labels) || 'условие'
}

// Мелкая подпись: что именно проверяли (название характеристики), если оно отличается от слов гостя.
export function itemSubtitle(item, labels) {
  if (!item) return ''
  // «Рядом место типа X» и «доехать до X» — без характеристики каталога, но проверены по картам.
  if (item.nearby || item.place) return 'по местам на карте'
  if (item.trip || String(item.reason_code || '').startsWith('trip_')) return 'оценка по прямой'
  if (item.reason_code === 'out_of_catalog' || (!item.feature_id && !item.items?.length && !item.op)) return 'не проверяем — такого пока нет в каталоге'
  const name = item.feature_name || featureName(item.feature_id, labels)
  const title = itemTitle(item, labels)
  return name && name.toLowerCase() !== title.toLowerCase() ? name : ''
}

export function verdictKind(value) {
  if (value === 'yes' || value === 'fits') return 'yes'
  if (value === 'no' || value === 'not_fit') return 'no'
  return 'unknown'
}

// Размещения по этажам: ответ «окна на запад» — по размещению на каждую стену (D41-3), номер на
// одной из них. Этаж «да» — только если «да» у всех его стен, «нет» — если у всех «нет».
export function placementFloors(placements) {
  const floors = new Map()
  for (const p of placements || []) {
    const key = `${p.building_id ?? ''}|${p.floor_label || p.floor}`
    const floor = floors.get(key) || { key, building_id: p.building_id, building_name: p.building_name, floor: p.floor, floor_label: p.floor_label, kinds: new Set(), walls: 0 }
    floor.kinds.add(verdictKind(p.verdict))
    floor.walls += 1
    floors.set(key, floor)
  }
  return [...floors.values()].map(({ kinds, ...floor }) => ({ ...floor, verdict: kinds.size === 1 ? [...kinds][0] : 'unknown' }))
}

export function itemMarks(item) {
  const statuses = new Set(item?.statuses || [])
  for (const child of item?.items || []) for (const status of child.statuses || []) statuses.add(status)
  return MARK_ORDER.filter((status) => statuses.has(status)).map((status) => STATUS_MARKS[status])
}

export function hasEstimate(item) {
  return (item?.statuses || []).includes('estimate')
}

// Вердикт условия словами (не статус отеля): «да», «нет», «нет данных». У оценки — «похоже, да».
export function verdictText(item) {
  const kind = verdictKind(item?.verdict)
  if (kind === 'yes') return hasEstimate(item) ? 'похоже, да' : 'да'
  if (kind === 'no') return item?.reason || 'нет'
  return item?.reason || 'нужно уточнить'
}

// Варианты группы «ИЛИ»: «поздний заезд: стойка — да; или самозаселение — нет данных».
export function variantsText(item, labels) {
  if (!item?.items?.length) return ''
  const joiner = item.op === 'AND' ? ' и ' : item.op === 'NOT' ? ', ' : ' или '
  return item.items.map((child) => `${child.feature_name || itemTitle(child, labels)} — ${shortVerdict(child)}`).join(joiner)
}

export function shortVerdict(item) {
  const kind = verdictKind(item?.verdict)
  return kind === 'yes' ? 'да' : kind === 'no' ? 'нет' : 'нет данных'
}

// Пункты карточки: сначала обязательные, потом желательные, с пометкой.
export function orderedItems(result) {
  return [
    ...(result?.hard || []).map((item) => ({ ...item, required: true })),
    ...(result?.soft || []).map((item) => ({ ...item, required: false })),
  ]
}

// Одни слова гостя на два условия («тихий» — дороги и бары): к повтору — название характеристики.
export function distinctTitles(items, labels) {
  const counts = new Map()
  for (const item of items) counts.set(itemTitle(item, labels), (counts.get(itemTitle(item, labels)) || 0) + 1)
  return items.map((item) => {
    const title = itemTitle(item, labels)
    const name = item.feature_name || featureName(item.feature_id, labels)
    return counts.get(title) > 1 && name && name !== title ? `${title} · ${name}` : title
  })
}

// Служебные пометки API для команды (номера решений, имена) гостю не показываем.
export function guestNotes(notes) {
  return (notes || []).filter((note) => !/\bD\d{1,3}\b|агент Данила|заглушк/i.test(note))
}

// Часть запроса вне каталога: что с ней делает поиск. «Рядом место типа X» проверяем по местам
// карты, «доехать до X» — оценкой по прямой, остальное — не проверяем (журнал спроса).
export function outOfCatalogCheck(item) {
  const place = item?.place
  if (place && !place.on_site && place.radius_m) {
    const quality = place.quality ? ' · «' + place.quality + '» не проверить — рейтингов нет' : ''
    return { checked: true, note: 'проверяем по карте · ' + place.label + ', до ' + place.radius_m + ' м' + quality }
  }
  if ((item?.nearest_contracts || []).includes('trip_to_target')) return { checked: true, note: 'оценка по прямой · время на транспорте не считаем' }
  const where = place ? place.label + (place.on_site ? ' в отеле' : '') : ''
  return { checked: false, note: (item?.mapping_status === 'partial' ? 'понято частично · не проверяем' : 'не проверяем') + (where ? ' · ' + where : '') }
}
