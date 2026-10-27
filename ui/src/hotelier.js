// Кабинет отельера: подтверждение сведений со страницы Островка и то, что понял агент-отельер.
import { valueWord } from './facts.js'

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
