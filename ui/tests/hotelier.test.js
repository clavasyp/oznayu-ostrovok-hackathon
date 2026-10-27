import test from 'node:test'
import assert from 'node:assert/strict'
import { agentFields, confirmText } from '../src/hotelier.js'

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
