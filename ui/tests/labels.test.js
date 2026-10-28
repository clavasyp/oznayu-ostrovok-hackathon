import test from 'node:test'
import assert from 'node:assert/strict'
import { areaText, compareRows, distinctTitles, guestNotes, hotelKind, itemMarks, itemSubtitle, itemTitle, orderedItems, outOfCatalogCheck, placeNote, placementFloors, shortVerdict, verdictKind, verdictText } from '../src/labels.js'

const labels = { features: { P01: 'можно с животным', L04: 'заселение в нужное время', L01: 'аптека, открытая в момент приезда', N02: 'далеко от оживлённых дорог', N04: 'мало баров и клубов рядом', B02: 'температура моря за месяц' } }

// Пункты из POST /api/search (fixtures): «Поздний заезд», «Посмотреть город», «без собак», «Море в июле».
const lateResult = {
  hard: [
    { op: 'OR', atom_id: null, feature_id: undefined, text_span: 'поздний заезд', label: 'поздний заезд', feature_name: null, verdict: 'unknown', reason: 'Об этом у нас пока нет данных', items: [
      { feature_id: 'L04', atom_id: 'a1', label: 'поздний заезд', feature_name: 'заселение в нужное время', verdict: 'unknown' },
      { feature_id: 'L04', atom_id: 'a2', label: 'поздний заезд', feature_name: 'заселение в нужное время (самозаселение)', verdict: 'yes', statuses: ['simulated_hotelier'] },
    ] },
    { feature_id: 'L01', atom_id: 'a3', label: 'открытая аптека рядом', feature_name: 'аптека, открытая в момент приезда', verdict: 'unknown', reason: 'Подходящее место с пешим путём по карте не найдено' },
  ],
  soft: [],
}
const cityResult = { hard: [{ feature_id: null, atom_id: 'a1', text_span: 'доехать до Большого театра', label: 'доехать до Большого театра', verdict: 'unknown', reason_code: 'out_of_catalog', reason: 'Такое мы пока не знаем' }], soft: [] }

test('titles come from the API words, never raw codes or empty strings', () => {
  for (const item of [...orderedItems(lateResult), ...orderedItems(cityResult)]) {
    const title = itemTitle(item, labels)
    assert.ok(title.trim(), 'пустая подпись')
    assert.doesNotMatch(title, /^[A-Z]\d\d$/)
  }
  assert.equal(itemTitle({ feature_id: 'P01', label: 'без собак', feature_name: 'можно с животным' }, labels), 'без собак')
  assert.equal(itemTitle({ feature_id: 'P01' }, labels), 'можно с животным')
  assert.equal(itemSubtitle({ feature_id: 'P01', label: 'без собак', feature_name: 'можно с животным' }, labels), 'можно с животным')
  assert.match(itemSubtitle(cityResult.hard[0], labels), /не проверяем/)
  assert.equal(itemSubtitle(lateResult.hard[0], labels), '')
})

test('verdict icons and words follow the verdict, not the hotel status', () => {
  assert.deepEqual(['yes', 'fits', 'no', 'not_fit', 'unknown', 'needs_check', undefined].map(verdictKind), ['yes', 'yes', 'no', 'no', 'unknown', 'unknown', 'unknown'])
  assert.equal(verdictText({ verdict: 'yes' }), 'да')
  assert.equal(verdictText({ verdict: 'yes', statuses: ['estimate'] }), 'похоже, да')
  assert.equal(verdictText({ verdict: 'no' }), 'нет')
  assert.equal(verdictText({ verdict: 'unknown', reason: 'Нет данных' }), 'Нет данных')
  assert.equal(shortVerdict({ verdict: 'unknown' }), 'нет данных')
})

test('estimate and simulator answers carry visible marks (D18, D31), also inside OR groups', () => {
  assert.deepEqual(itemMarks({ statuses: ['estimate'] }), ['оценка'])
  assert.deepEqual(itemMarks({ statuses: ['computed', 'simulated_hotelier'] }), ['ответ имитатора отельера'])
  assert.deepEqual(itemMarks(lateResult.hard[0]), ['ответ имитатора отельера'])
  assert.deepEqual(itemMarks({ statuses: ['computed'] }), [])
})

test('required items come first and repeated guest words get the feature name', () => {
  const items = orderedItems({ hard: [{ feature_id: 'V05', label: 'видом на закат' }], soft: [{ feature_id: 'N02', label: 'тихий', feature_name: 'далеко от оживлённых дорог' }, { feature_id: 'N04', label: 'тихий', feature_name: 'мало баров и клубов рядом' }] })
  assert.deepEqual(items.map((item) => item.required), [true, false, false])
  assert.deepEqual(distinctTitles(items, labels), ['видом на закат', 'тихий · далеко от оживлённых дорог', 'тихий · мало баров и клубов рядом'])
})

test('service notes for the team are hidden from the guest; hotel kinds are words', () => {
  const notes = ['Условия уровня окна … по размещениям категорий (D17) — все вместе', 'Запрос разобран правилами (офлайн-режим): ключ LLM не задан.', 'Разбор текста — заглушка. Настоящий разбор — агент Данила.']
  assert.deepEqual(guestNotes(notes), ['Запрос разобран правилами (офлайн-режим): ключ LLM не задан.'])
  assert.deepEqual(['RESORT', 'GUESTHOUSE', 'HOSTEL', undefined, 'SOMETHING_NEW'].map(hotelKind), ['Курорт', 'Гостевой дом', 'Хостел', 'Отель', 'Жильё'])
})

test('placements on several walls of one floor are one row; the floor is «yes» only on every wall', () => {
  const p = (floor, wall, verdict) => ({ placement_id: `rt|b|${floor}|seg-${wall}`, building_id: 'b', floor, verdict })
  const floors = placementFloors([p(3, 'a', 'yes'), p(3, 'b', 'no'), p(4, 'a', 'yes'), p(4, 'b', 'yes'), p(5, 'a', 'no')])
  assert.deepEqual(floors.map((f) => [f.floor, f.verdict, f.walls]), [[3, 'unknown', 2], [4, 'yes', 2], [5, 'no', 1]])
})

test('places and trips are checked by the map, the rest of out-of-catalog is not', () => {
  const place = { text_span: 'рядом с хорошими ресторанами', place: { label: 'ресторан', radius_m: 800, quality: 'хорошими' } }
  assert.equal(outOfCatalogCheck(place).checked, true)
  assert.match(outOfCatalogCheck(place).note, /проверяем по карте · ресторан, до 800 м · «хорошими» не проверить/)
  assert.equal(outOfCatalogCheck({ text_span: 'доехать до Большого театра', nearest_contracts: ['trip_to_target'] }).checked, true)
  assert.equal(outOfCatalogCheck({ text_span: 'чистота' }).checked, false)
  assert.equal(itemSubtitle({ text_span: 'рядом с парком', nearby: { category: 'park' }, verdict: 'yes' }, {}), 'по местам на карте')
})

test('an amenity in the hotel is checked on the hotel page, not on the map', () => {
  const pool = { text_span: 'с бассейном', place: { label: 'бассейн', on_site: true } }
  assert.equal(outOfCatalogCheck(pool).checked, true)
  assert.match(outOfCatalogCheck(pool).note, /по странице отеля · бассейн в отеле/)
  const found = { text_span: 'с бассейном', nearby: { category: 'pool' }, on_site: ['Открытый бассейн'], verdict: 'yes' }
  assert.equal(itemSubtitle(found, {}), 'по странице отеля на Островке')
  assert.equal(itemSubtitle({ text_span: 'с бассейном', nearby: { category: 'pool' }, reason_code: 'place_on_site' }, {}), 'в самом отеле')
})

test('the Sochi area filter is shown in guest words', () => {
  assert.equal(areaText({ areas: ['adler'] }), 'только Адлер')
  assert.equal(areaText({ exclude_areas: ['adler', 'sirius'] }), 'кроме: Адлер, Сириус')
  assert.equal(areaText({}), '')
  assert.equal(areaText({ areas: ['khamovniki'] }, { areas: { khamovniki: 'Хамовники' } }), 'только Хамовники')
  assert.equal(areaText({ areas: ['adler'], kinds: ['HOSTEL'], stars_min: 4 }), 'только Адлер; хостелы; от 4★ на Островке')
})

test('comparison puts the same condition of each hotel in one row, required first', () => {
  const a = { hard: [{ atom_id: 'a1', label: 'лифт', verdict: 'yes' }], soft: [{ atom_id: 'a2', label: 'у моря', verdict: 'unknown', reason: 'есть только оценка' }] }
  const b = { hard: [{ atom_id: 'a1', label: 'лифт', verdict: 'no', reason: 'Островок: «Отсутствие лифтов»' }], soft: [{ atom_id: 'a2', label: 'у моря', verdict: 'yes' }] }
  const rows = compareRows([a, b], {})
  assert.deepEqual(rows.map((r) => [r.title, r.required]), [['лифт', true], ['у моря', false]])
  assert.deepEqual(rows[0].cells.map((c) => c.kind), ['yes', 'no'])
  assert.equal(rows[0].cells[1].text, 'Островок: «Отсутствие лифтов»')
  assert.equal(rows[1].cells[0].text, 'есть только оценка')
  // С объяснением — его текст по условию, со значением, без названия условия.
  const said = { ...a, explanation: { claims: [{ atom_id: 'a2', text: 'У моря: нужно уточнить — 576 м, есть только оценка' }] } }
  assert.equal(compareRows([said, b], {})[1].cells[0].text, '576 м, есть только оценка')
})

test('a catalog place feature shows the found place, distance and hours at the guest moment', () => {
  const hours = { state: 'open', opening_hours: '24/7', moment: { arrival_local: '2027-05-14T01:00:00' } }
  const food = { feature_id: 'L02', text_span: 'открыта еда', nearby: { category: 'L02', radius_m: 500 }, verdict: 'yes', place: { place_id: 'osm:node/1', name: 'Шаурма у вокзала', distance_m: 240, hours } }
  assert.equal(itemSubtitle(food, labels), 'по местам на карте · «Шаурма у вокзала», 240 м по прямой · открыто: 24/7')
  const closed = { ...food, verdict: 'unknown', place: { ...food.place, name: null, hours: { ...hours, state: 'closed', opening_hours: 'Mo-Su 09:00-22:00' } } }
  assert.equal(itemSubtitle(closed, labels), 'по местам на карте · место без названия, 240 м по прямой · часы: Mo-Su 09:00-22:00')
  assert.equal(placeNote({ label: 'ресторан', radius_m: 800 }), '')  // часть запроса из AST — не найденное место
})
