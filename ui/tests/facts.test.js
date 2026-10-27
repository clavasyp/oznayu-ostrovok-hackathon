import test from 'node:test'
import assert from 'node:assert/strict'
import { factRows, formatFactValue, hotelierSource, paramWords, windowsText } from '../src/facts.js'

const window = (id, status, value) => ({ fact_id: id, feature_id: 'V05', subject_level: 'facade_physical_floor', subject_id: `b:${id}:floor3`, status, value })

test('window facts collapse into one line per feature; ordinary facts stay one per row', () => {
  const facts = {
    V05: [window('w1', 'computed', { visible: true, why: 'clear' }), window('w2', 'computed', { visible: false, why: 'blocked' }), window('w3', 'estimate', { visible: true }), window('w4', 'unknown', null)],
    N04: [{ fact_id: 'n1', feature_id: 'N04', subject_level: 'hotel', status: 'computed', value: { count: 1 } }],
  }
  const rows = factRows(facts, new Set(['w2']))
  assert.deepEqual(rows.map((row) => row.kind), ['windows', 'fact'])
  assert.deepEqual(rows[0].tally, { yes: 1, no: 1, estimate: 1, unknown: 1 })
  assert.equal(windowsText(rows[0]), '4 окна: «да» по расчёту — 1, «нет» по расчёту — 1, оценка — 1, неизвестно — 1')
  const many = factRows({ V01: Array.from({ length: 3170 }, (_, i) => window(`v${i}`, 'estimate', { visible: true })) })
  assert.equal(many.length, 1)
  assert.match(windowsText(many[0]), /^3170 окон/)
})

test('values and params are words, never JSON', () => {
  const values = [
    { value: { visible: false, why: 'blocked' } },
    { value: { dsm_positive_dz_estimate_m: 1.8, ok: true } },
    { value: { known_step_sections: 0, mapped_steps_length_m: 0 } },
    { value: { known_crossings: 2, crossings: [] } },
    { value: { known_lit_percent: 0, unknown_percent: 100 } },
    { value: { species: 'dog' } },
    { value: { nested: { deep: 1 } } },
    { value: { mean_C: 26.27 }, unit: '°C' },
    { value: 8.4, unit: '%' },
  ].map((fact) => formatFactValue(fact))
  assert.deepEqual(values, ['не видно — закрыто зданием', 'подъём ≈ 1,8 м', 'лестниц: 0', 'переходов: 2', 'освещено 0 %, неизвестно 100 %', 'собаки', 'подробности в паспорте', '26,3 °C', '8,4 %'])
  for (const value of values) assert.doesNotMatch(value, /[{}[\]"]/)
  assert.equal(formatFactValue({ value: { hvac_type: 'split', individual_control: true } }, { hvac_type: 'тип кондиционера', individual_control: 'гость регулирует сам, да/нет' }), 'тип кондиционера: сплит-система · гость регулирует сам: да')
  const params = paramWords({ month: 7, grid_latitude: 43.5, archive: 'x', dataset_sha256: 'abc', target_kind: 'beach', radius_m: 300, selection_protocol: 'p' })
  assert.deepEqual(params, [['Месяц', 'июль'], ['Цель', 'пляж'], ['Радиус', '300 м']])
})

test('a hotel answer shows its quote, question and time', () => {
  const fact = { status: 'simulated_hotelier', observed_at: '2026-10-27T13:54:44Z', sources: [{ provider: 'hotelier_simulator', question_id: '307854513098104549', answered_at: '2026-10-27T13:54:44+00:00', respondent: 'Песочница', quote: 'Да, в номерах сплит-системы.' }] }
  const source = hotelierSource(fact)
  assert.equal(source.quote, 'Да, в номерах сплит-системы.')
  assert.equal(source.questionId, '307854513098104549')
  assert.equal(source.simulated, true)
  assert.match(source.answeredAt, /27 октября 2026/)
  assert.equal(hotelierSource({ status: 'computed', sources: [{ provider: 'OpenStreetMap' }] }), null)
})
