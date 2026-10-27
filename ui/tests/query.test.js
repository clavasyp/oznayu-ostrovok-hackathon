import test from 'node:test'
import assert from 'node:assert/strict'
import { PAGE_SIZE, clarifyKind, filterTotal, pageWindow, refineQuery, searchCounts, searchTotal, serverPaged, statusStart } from '../src/query.js'

const result = (status) => ({ status, hotel: { hotel_id: status } })

test('without total/counts the whole answer is one page: counts come from the results', () => {
  const data = { results: [result('fits'), result('needs_check'), result('needs_check'), result('not_fit')] }
  assert.equal(serverPaged(data), false)
  assert.deepEqual(searchCounts(data), { fits: 1, needs_check: 2, not_fit: 1 })
  assert.equal(searchTotal(data), 4)
  assert.equal(filterTotal('needs_check', data), 2)
  assert.equal(PAGE_SIZE, 30)
})

test('with total/counts the filter asks the server from where its status starts', () => {
  const data = { total: 2695, counts: { fits: 40, needs_check: 2600, not_fit: 55 }, results: Array.from({ length: PAGE_SIZE }, () => result('fits')) }
  assert.equal(serverPaged(data), true)
  assert.equal(searchTotal(data), 2695)
  assert.equal(filterTotal('all', data), 2695)
  assert.equal(statusStart('fits', data.counts), 0)
  assert.equal(statusStart('needs_check', data.counts), 40)
  assert.deepEqual(pageWindow('not_fit', data), { start: 2640, total: 55 })
  assert.deepEqual(pageWindow('all', data), { start: 0, total: 2695 })
})

const quiet = { question: '«Тихий» — важна тишина в самом номере или подальше от дорог и баров?', about: 'тихий', options: ['в номере', 'подальше от дорог и баров', 'и то и другое'] }
const text = 'тихий номер с видом на закат недалеко от моря'

test('quiet clarification is answered inside the same sentence, without comparatives that change requiredness', () => {
  assert.equal(clarifyKind(quiet), 'quiet')
  assert.equal(refineQuery(text, quiet, 'в номере').text, 'тихий (в номере) номер с видом на закат недалеко от моря')
  assert.equal(refineQuery(text, quiet, 'подальше от дорог и баров').text, 'тихий (снаружи) номер с видом на закат недалеко от моря')
  assert.equal(refineQuery(text, quiet, 'и то и другое').text, 'тихий (в номере) номер с видом на закат недалеко от моря. Снаружи тоже тихо.')
  assert.doesNotMatch(refineQuery(text, quiet, 'подальше от дорог и баров').text, /подальше|обязательно|лучше/)
})

test('other clarifications: dates open the trip picker, «нет» just closes the question', () => {
  assert.equal(clarifyKind({ about: 'даты поездки', question: 'На какие даты поездка?', options: [] }), 'dates')
  const ask = { question: 'Учитывать «вид на море из окна»?', about: 'море', options: ['да', 'нет'] }
  assert.equal(clarifyKind(ask), 'confirm')
  assert.equal(refineQuery('номер у моря', ask, 'да').text, 'номер у моря. Вид на море из окна.')
  assert.deepEqual(refineQuery('номер у моря', ask, 'нет'), { text: null, note: 'Не учитываем — выдача не меняется' })
  const negated = { question: '«без ковров» — что именно не подходит: ковролин?', about: 'без ковров', options: ['да, этого не нужно', 'неважно'] }
  assert.equal(clarifyKind(negated), 'negated')
  assert.equal(refineQuery('номер без ковров', negated, 'неважно').text, null)
})
