import test from 'node:test'
import assert from 'node:assert/strict'
import { addIsoDays, nightCount, stayError, formatDateRange, formatGuests, formatNights } from '../src/trip.js'

test('calendar nights remain exact across month, year and daylight saving changes', () => {
  assert.equal(nightCount('2026-12-30', '2027-01-03'), 4)
  assert.equal(nightCount('2026-03-28', '2026-03-30'), 2)
  assert.equal(nightCount('2028-02-28', '2028-03-01'), 2)
  assert.equal(addIsoDays('2028-02-28', 1), '2028-02-29')
  assert.equal(addIsoDays('2026-12-31', 1), '2027-01-01')
})

test('invalid dates and guest counts cannot reach confirmation', () => {
  const stay = { checkIn: '2026-11-14', checkOut: '2026-11-17', adults: 2, children: 0 }
  assert.equal(stayError(stay, '2026-10-25'), '')
  for (const change of [{ checkIn: '' }, { checkIn: '2026-02-30' }, { checkOut: '2026-11-14' }, { checkOut: '2026-11-13' }, { adults: 0 }, { children: 7 }]) assert.ok(stayError({ ...stay, ...change }, '2026-10-25'))
  assert.ok(stayError(stay, '2026-11-15'))
  assert.equal(nightCount('bad', '2026-11-17'), 0)
})

test('trip summary identifies both years and uses correct Russian plurals', () => {
  assert.equal(formatDateRange('2026-11-14', '2026-11-17', true), '14–17 ноября 2026')
  assert.match(formatDateRange('2026-12-30', '2027-01-03'), /2026.*2027/)
  assert.equal(formatGuests(1, 2), '1 взрослый · 2 ребёнка')
  assert.equal(formatGuests(3, 5), '3 взрослых · 5 детей')
  assert.equal(formatNights(1), '1 ночь')
  assert.equal(formatNights(4), '4 ночи')
  assert.equal(formatNights(11), '11 ночей')
  assert.equal(formatNights(21), '21 ночь')
})

import { conditionPeriod, factPeriod, hasTripDates, periodMismatch, stayFromAst, todayIso, tripDatesText, withTripDates } from '../src/trip.js'

test('chosen dates reach the search through the query text the parser understands', () => {
  const stay = { checkIn: '2026-11-10', checkOut: '2026-11-13', adults: 2, children: 0 }
  assert.equal(tripDatesText(stay.checkIn, stay.checkOut), '10–13 ноября 2026')
  assert.equal(tripDatesText('2026-12-30', '2027-01-03'), '30 декабря 2026 — 3 января 2027')
  assert.equal(withTripDates('тихий номер с видом на закат', stay), 'тихий номер с видом на закат, 10–13 ноября 2026')
  // Даты в тексте заменяются, а не дописываются вторыми: разбор берёт первые.
  assert.equal(withTripDates('закат из окна 10–15 июля вдвоём', stay), 'закат из окна 10–13 ноября 2026 вдвоём')
  assert.equal(withTripDates('закат 30 декабря 2026 — 3 января 2027', { ...stay, checkIn: '2027-01-05', checkOut: '2027-01-07' }), 'закат 5–7 января 2027')
  assert.equal(withTripDates('номер с 10 по 12 ноября у моря', stay), 'номер 10–13 ноября 2026 у моря')
  assert.equal(withTripDates('номер с 28 октября по 2 ноября', stay), 'номер 10–13 ноября 2026')
  assert.equal(withTripDates('вес 10 кг, номер у моря', stay), 'вес 10 кг, номер у моря, 10–13 ноября 2026')
  assert.ok(hasTripDates('море 10 июля'))
  assert.ok(!hasTripDates('море в июле'))
  assert.equal(withTripDates('', stay), '')
})

test('dates and guests from the parsed query become the trip', () => {
  const stay = { checkIn: '2026-11-10', checkOut: '2026-11-13', adults: 1, children: 0 }
  const next = stayFromAst({ dates: { from: '2027-07-10', to: '2027-07-15' }, guests: { adults: 2, children_ages: [5], pets: [] } }, stay, '2026-10-27')
  assert.deepEqual(next, { checkIn: '2027-07-10', checkOut: '2027-07-15', adults: 2, children: 1 })
  // Без «по» разбор знает только заезд: выезд гостя остаётся, если заезд тот же.
  assert.equal(stayFromAst({ dates: { from: '2026-11-10', to: null } }, stay, '2026-10-27').checkOut, '2026-11-13')
  assert.equal(stayFromAst({ dates: { from: '2026-12-30', to: null } }, stay, '2026-10-27').checkOut, '2026-12-31')
  assert.equal(stayFromAst({ dates: { from: '2026-10-01', to: '2026-10-05' } }, stay, '2026-10-27'), stay)
  assert.equal(stayFromAst({}, stay, '2026-10-27'), stay)
})

test('a condition computed for another month or date is flagged against the trip', () => {
  const november = { checkIn: '2026-11-10', checkOut: '2026-11-13', adults: 2, children: 0 }
  assert.equal(conditionPeriod({ month: 7 }), 'июль')
  assert.equal(conditionPeriod({ month: '7' }), 'июль')
  assert.equal(conditionPeriod({ date: '2027-07-10' }), '10 июля')
  assert.equal(periodMismatch({ month: 7 }, november), 'Посчитано для июля, а поездка — 10–13 ноября 2026')
  assert.equal(periodMismatch({ month: 11 }, november), '')
  assert.equal(periodMismatch({ date: '2026-11-11' }, november), '')
  assert.match(periodMismatch({ date: '2027-07-10' }, november), /Посчитано на 10 июля 2027/)
  assert.equal(periodMismatch({}, november), '')
})

test('today is taken in the city time zone and cross-year ranges keep both years without a stray «г»', () => {
  const instant = new Date('2026-12-31T21:30:00Z')
  assert.equal(todayIso(instant, 'Asia/Dubai'), '2027-01-01')
  assert.equal(todayIso(instant, 'Europe/Kaliningrad'), '2026-12-31')
  assert.equal(formatDateRange('2026-12-30', '2027-01-03'), '30 дек 2026 — 3 янв 2027')
  assert.equal(formatDateRange('2026-11-28', '2026-12-02', true), '28 ноября — 2 декабря 2026')
})

test('fact period: own date or month, trip start for seasonal routes', () => {
  assert.equal(factPeriod({ month: 7 }), 'июль')
  assert.equal(factPeriod({ trip_start: '2026-11-14', trip_end: '2026-11-17' }), 'ноябрь')
  assert.equal(factPeriod({}), '')
})
