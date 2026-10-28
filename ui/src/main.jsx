import React, { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import L from 'leaflet'
import {
  ArrowDownUp, ArrowRight, BedDouble, CalendarDays, Check,
  CheckCircle2, ChevronDown, CircleHelp, Columns3, Compass, ExternalLink,
  Heart, Lock, MapPin, MessageCircle, Minus, Plus, RotateCcw,
  Search, ShieldCheck, SlidersHorizontal, Sparkles, Users, X,
} from 'lucide-react'
import 'leaflet/dist/leaflet.css'
import './styles.css'
import { addIsoDays, cityToday, conditionPeriod, defaultStay, formatDateRange, formatDay, factPeriod, formatGuests, formatNights, hasTripDates, nightCount, periodMismatch, plural, stayError, stayFromAst, withTripDates } from './trip.js'
import { useDialog } from './useDialog.js'
import { groupMapPoints } from './mapClusters.js'
import { applyEdits, conditionChips, editOf, moveCondition, recordEdit } from './astEdit.js'
import { PROVENANCE, STATUS_LABELS, areaText, compareRows, distinctTitles, featureName, guestNotes, hasEstimate, hotelKind, itemMarks, itemSubtitle, itemTitle, orderedItems, outOfCatalogCheck, placementFloors, shortVerdict, verdictKind, verdictText } from './labels.js'
import { PAGE_SIZE, clarifyKey, clarifyKind, filterTotal, pageWindow, refineQuery, searchCounts, serverPaged } from './query.js'
import { attemptStateText, confidenceText, factRows, formatDateTime, formatFactValue, formatNumber, hotelierSource, paramWords, providerText, subjectLevelText, windowsText } from './facts.js'
import { ANSWER_STATES, agentFields, buildingLabel, confirmText, marksFromAliases, osmUrl, placementBody, placementOutcome, placementQuestionText, placementState, questionAfter, searchCheck, wallSides } from './hotelier.js'

const API = '/api'
const DEFAULT_QUERY = 'тихий номер с видом на закат недалеко от моря'
const cityNames = { sochi: 'Большой Сочи', moscow: 'Москва', spb: 'Петербург', kazan: 'Казань', kaliningrad: 'Калининград', minsk: 'Минск', istanbul: 'Стамбул', dubai: 'Дубай' }
// Центр карты до выдачи — для каждого города, а не «Москва или Сочи».
const CITY_VIEWS = {
  sochi: [[43.585, 39.723], 10], moscow: [[55.751, 37.617], 11], spb: [[59.939, 30.316], 11], kazan: [[55.796, 49.106], 11],
  kaliningrad: [[54.71, 20.51], 12], minsk: [[53.902, 27.561], 11], istanbul: [[41.015, 28.979], 11], dubai: [[25.205, 55.27], 11],
}
const scenarioPresets = [
  { label: 'Закат и море', city: 'sochi', text: DEFAULT_QUERY },
  { label: 'Посмотреть город', city: 'moscow', text: 'доехать до Большого театра и рядом итальянский ресторан' },
  { label: 'С ребёнком', city: 'moscow', text: 'едем с ребёнком, нужна детская площадка поблизости и кроватка в номере' },
  { label: 'С питомцем', city: 'sochi', text: 'можно с собакой и рядом ветеринарная клиника' },
  { label: 'Поздний заезд', city: 'sochi', text: 'приеду в 02:00, нужен поздний заезд и открытая аптека рядом' },
  { label: 'Для удалённой работы', city: 'moscow', text: 'тихий номер с быстрым Wi-Fi и удобным столом' },
  { label: 'Без ступеней', city: 'sochi', text: 'маршрут до пляжа без ступеней, лифт и доступный вход' },
  { label: 'Море в июле', city: 'sochi', text: 'хочу поехать в июле, какая температура моря и пляж рядом' },
]
const FILTERS = [['all', 'Все'], ['fits', 'Подходят'], ['needs_check', 'Уточнить'], ['not_fit', 'Не подходят']]
// Ближайшие контракты журнала «вне каталога», которые не характеристики каталога.
const CONTRACTS = { nearby_place: 'место рядом', trip_to_target: 'доехать до места' }

const LabelsContext = createContext({ features: {} })
const useLabels = () => useContext(LabelsContext)

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } })
  if (!response.ok) {
    const error = new Error(`API: ${response.status}`)
    error.status = response.status
    throw error
  }
  return response.json()
}
const isAbort = (error) => error?.name === 'AbortError'

function searchBody(text, cityId, ast, offset = 0) {
  const body = { text, city_id: cityId, limit: PAGE_SIZE, offset }
  if (ast) body.ast = { ...ast, city_id: cityId }
  return body
}

function firstPage(data) {
  const items = data?.results || []
  return { status: 'all', start: 0, fetched: items.length, items, shown: serverPaged(data) ? Infinity : PAGE_SIZE, loading: false }
}
const EMPTY_LIST = { status: 'all', start: 0, fetched: 0, items: [], shown: PAGE_SIZE, loading: false }

// Параметры условия из разобранного запроса (месяц у температуры моря, дата у заката).
function conditionParams(ast, atomId) {
  if (!ast || !atomId) return {}
  return ([...(ast.hard || []), ...(ast.soft || [])].find((item) => item.atom_id === atomId) || {}).params || {}
}

function VerdictIcon({ value, size = 13 }) {
  const kind = verdictKind(value)
  if (kind === 'yes') return <Check size={size} aria-hidden="true" />
  if (kind === 'no') return <X size={size} aria-hidden="true" />
  return <CircleHelp size={size} aria-hidden="true" />
}

function Marks({ item }) {
  const marks = itemMarks(item)
  return marks.length ? <span className="mark-list">{marks.map((mark) => <i key={mark} className={`item-mark ${mark.startsWith('ответ имитатора') ? 'simulated' : mark === 'оценка' ? 'estimate' : ''}`}>{mark}</i>)}</span> : null
}

function App() {
  const [section, setSection] = useState('guest')
  const [city, setCity] = useState('sochi')
  const [cities, setCities] = useState([])
  const [labels, setLabels] = useState({ booked: 'Забронировано', booking_imitation_note: 'Имитация оформления Островка. Реальная бронь не создаётся', out_of_catalog: 'Такое мы пока не знаем', features: {} })
  const [query, setQuery] = useState(DEFAULT_QUERY)
  const [searchState, setSearchState] = useState('idle')
  const [searchError, setSearchError] = useState('')
  const [searchData, setSearchData] = useState(null)
  const [searchVersion, setSearchVersion] = useState(0)
  const [list, setList] = useState(EMPTY_LIST)
  const [edits, setEditsState] = useState([])
  const [selected, setSelected] = useState(null)
  const [hotelData, setHotelData] = useState(null)
  const [hotelLoading, setHotelLoading] = useState(false)
  const [explanations, setExplanations] = useState({})
  const [booking, setBooking] = useState(false)
  const [booked, setBooked] = useState(false)
  const [stay, setStay] = useState(() => defaultStay(cityToday('sochi')))
  const [showMap, setShowMap] = useState(true)
  const [saved, setSaved] = useState([])
  const [toast, setToast] = useState('')
  const [hotelierAnswers, setHotelierAnswers] = useState({})
  const [activeHotel, setActiveHotel] = useState('')
  // Вкладка кабинета («Об отеле» / «Где окна номеров») переживает переход в поиск и обратно.
  const [hotelierTab, setHotelierTab] = useState('questions')
  const searchSeq = useRef(0)
  const searchAbort = useRef(null)
  const pageSeq = useRef(0)
  const hotelSeq = useRef(0)
  const toastTimer = useRef(0)
  const searchRef = useRef(null)
  const editsRef = useRef([])
  searchRef.current = searchData

  // Правки разбора гостя — в ref тоже: поиск сразу после правки должен видеть новые.
  function setEdits(next) {
    editsRef.current = typeof next === 'function' ? next(editsRef.current) : next
    setEditsState(editsRef.current)
  }

  useEffect(() => {
    api('/cities').then((d) => setCities(d.cities || [])).catch(() => setCities([]))
    api('/meta/labels').then((data) => setLabels((current) => ({ ...current, ...data, features: data.features || current.features }))).catch(() => {})
    runSearch(DEFAULT_QUERY, 'sochi')
  }, [])

  const zoneOf = (cityId) => cities.find((item) => item.city_id === cityId)?.timezone

  function notify(message) {
    window.clearTimeout(toastTimer.current)
    setToast(message)
    toastTimer.current = window.setTimeout(() => setToast(''), 3400)
  }

  async function runSearch(text = query, cityId = city, { ast = null } = {}) {
    const trimmed = (text || '').trim()
    if (!trimmed && !ast) {
      notify('Опишите, что для вас важно в поездке')
      return
    }
    const seq = ++searchSeq.current
    pageSeq.current += 1
    searchAbort.current?.abort()
    const controller = new AbortController()
    searchAbort.current = controller
    setSearchState('loading')
    setSearchError('')
    setQuery(text)
    try {
      const post = (body) => api('/search', { method: 'POST', body: JSON.stringify(body), signal: controller.signal })
      let searched = trimmed
      let data = await post(searchBody(trimmed, cityId, ast))
      // Даты поездки из шапки: разбор спрашивает «на какие даты», а в тексте своих дат нет — ищем
      // с выбранными датами, как после «Применить даты» (закат и погода считаются на них).
      const needsDates = (data?.ast?.clarify || []).some((item) => clarifyKind(item) === 'dates')
      if (!ast && needsDates && !hasTripDates(trimmed)) {
        const dated = withTripDates(trimmed, stay)
        if (dated && dated !== trimmed) {
          searched = dated
          data = await post(searchBody(dated, cityId, null))
          if (seq !== searchSeq.current) return
          setQuery(dated)
        }
      }
      // Правки гостя переживают новый разбор: те же условия переносим снова и ищем по AST.
      if (!ast && editsRef.current.length && data?.ast) {
        const edited = applyEdits(data.ast, editsRef.current)
        if (edited.applied.length) data = await post(searchBody(searched, cityId, edited.ast))
      }
      if (seq !== searchSeq.current) return
      setSearchData(data)
      setSearchVersion((version) => version + 1)
      setList(firstPage(data))
      setExplanations({})
      setSearchState('ready')
      setStay((current) => stayFromAst(data.ast, current, cityToday(cityId, zoneOf(cityId))))
      setSelected((current) => {
        if (!current || current.hotel.city_id !== cityId) return current
        return (data.results || []).find((item) => item.hotel.hotel_id === current.hotel.hotel_id) || { ...current, stale: true }
      })
      return data
    } catch (error) {
      if (isAbort(error) || seq !== searchSeq.current) return
      setSearchData(null)
      setList(EMPTY_LIST)
      setSearchState(error.status ? 'error' : 'offline')
      setSearchError(error.status ? `Поиск ответил ошибкой ${error.status}. Попробуйте изменить запрос.` : '')
    }
  }

  async function fetchPage(status, offset, append) {
    const data = searchRef.current
    if (!data?.ast) return
    const seq = ++pageSeq.current
    setList((current) => ({ ...current, loading: true }))
    try {
      const page = await api('/search', { method: 'POST', body: JSON.stringify(searchBody(data.ast.raw_text, data.ast.city_id || city, data.ast, offset)) })
      if (seq !== pageSeq.current || searchRef.current !== data) return
      const raw = page.results || []
      const items = raw.filter((item) => status === 'all' || item.status === status)
      setList((current) => ({ status, start: append ? current.start : offset, fetched: (append ? current.fetched : 0) + raw.length, items: append ? [...current.items, ...items] : items, shown: Infinity, loading: false }))
    } catch {
      if (seq !== pageSeq.current) return
      setList((current) => ({ ...current, loading: false }))
      notify('Не удалось загрузить ещё варианты')
    }
  }

  function changeFilter(status) {
    const data = searchRef.current
    if (!data || status === list.status) return
    if (!serverPaged(data)) return setList((current) => ({ ...current, status, shown: PAGE_SIZE }))
    pageSeq.current += 1
    if (status === 'all') return setList(firstPage(data))
    const { start, total } = pageWindow(status, data)
    setList({ status, start, fetched: 0, items: [], shown: Infinity, loading: Boolean(total) })
    if (total) fetchPage(status, start, false)
  }

  function loadMore() {
    const data = searchRef.current
    if (!data || list.loading) return
    if (!serverPaged(data)) return setList((current) => ({ ...current, shown: current.shown + PAGE_SIZE }))
    fetchPage(list.status, list.start + list.fetched, true)
  }

  const openHotel = useCallback(async (result) => {
    const seq = ++hotelSeq.current
    setSelected(result)
    setHotelData(null)
    setHotelLoading(true)
    try {
      const data = await api(`/hotels/${result.hotel.hotel_id}`)
      if (seq === hotelSeq.current) setHotelData(data)
    } catch {
      if (seq === hotelSeq.current) setHotelData(null)
    } finally {
      if (seq === hotelSeq.current) setHotelLoading(false)
    }
  }, [])

  const toggleSave = useCallback((result) => {
    const id = result.hotel.hotel_id
    setSaved((items) => items.some((item) => item.hotel_id === id)
      ? items.filter((item) => item.hotel_id !== id)
      : [...items, { hotel_id: id, name: result.hotel.name, city_id: result.hotel.city_id, result: { ...result, _ast: result._ast || searchRef.current?.ast } }])
  }, [])
  const savedIds = useMemo(() => new Set(saved.map((item) => item.hotel_id)), [saved])

  // Объяснение: у первых результатов приходит с поиском, у остальных — POST /api/explain при открытии.
  const explanationKey = selected ? `${selected._ast ? 'saved' : searchVersion}:${selected.hotel.hotel_id}` : ''
  useEffect(() => {
    if (!selected || selected.explanation?.text || explanations[explanationKey]) return
    const ast = selected._ast || searchRef.current?.ast
    if (!ast) return
    const key = explanationKey
    setExplanations((items) => ({ ...items, [key]: { state: 'loading' } }))
    api('/explain', { method: 'POST', body: JSON.stringify({ ast, city_id: selected.hotel.city_id, hotel_id: selected.hotel.hotel_id }) })
      .then((data) => setExplanations((items) => ({ ...items, [key]: { state: 'ready', text: data.explanation?.text || '' } })))
      .catch(() => setExplanations((items) => ({ ...items, [key]: { state: 'error', text: '' } })))
  }, [explanationKey])

  function changeCity(nextCity, text = query) {
    setCity(nextCity)
    if (text.trim()) return runSearch(text, nextCity)
    searchSeq.current += 1
    setSearchData(null)
    setList(EMPTY_LIST)
    setSearchState('idle')
  }

  // Даты поездки уходят в поиск через текст запроса — так их учтут разбор и условия на дату.
  // «Применить даты» (`always`) вносит их в запрос и без смены; из оформления — только новые даты.
  function applyStay(next, { always = false } = {}) {
    const datesChanged = next.checkIn !== stay.checkIn || next.checkOut !== stay.checkOut
    setStay(next)
    if ((!datesChanged && !always) || !query.trim()) return
    const text = withTripDates(query, next)
    if (text && text !== query.trim()) runSearch(text, city)
  }

  // Пересчёт подборки гостя с тем же разбором; промис с новой выдачей (или null) — для кабинета.
  function refreshGuest() {
    const data = searchRef.current
    return data?.ast ? runSearch(data.ast.raw_text, data.ast.city_id || city, { ast: data.ast }) : Promise.resolve(null)
  }

  // «Проверить в поиске» из кабинета: подборка гостя и карточка отеля, если он на странице.
  // Поиск по другому городу — тот же запрос гостя в городе отеля.
  async function showInSearch(hotelId, cityId) {
    setSection('guest')
    let items = list.items
    if (searchRef.current?.ast?.city_id !== cityId) {
      setCity(cityId)
      const data = await runSearch(query, cityId)
      if (!data) return
      items = data.results || []
    }
    const result = items.find((item) => item.hotel.hotel_id === hotelId)
    if (result) openHotel(result)
    else notify('Отеля нет на первой странице подборки — уточните запрос или откройте фильтр «Подходят»')
  }

  const paged = serverPaged(searchData)
  const filtered = useMemo(() => list.status === 'all' ? list.items : list.items.filter((item) => item.status === list.status), [list.items, list.status])
  const visibleResults = useMemo(() => paged ? filtered : filtered.slice(0, list.shown), [filtered, paged, list.shown])
  const windowTotal = searchData ? pageWindow(list.status, searchData).total : 0
  const remaining = paged ? Math.max(windowTotal - list.fetched, 0) : Math.max(filtered.length - visibleResults.length, 0)
  const selectedCity = cities.find((item) => item.city_id === city) || { city_id: city, name: cityNames[city] || city, depth: 'automatic' }
  const selectedAst = selected?._ast || searchData?.ast

  function openSaved(item) {
    const fresh = list.items.find((result) => result.hotel.hotel_id === item.hotel_id)
    openHotel(fresh || item.result)
  }

  return (
    <LabelsContext.Provider value={labels}>
      <div className="app-shell">
        <header className="topbar">
          <a className="brand" href="#home" onClick={(e) => { e.preventDefault(); setSection('guest'); setSelected(null) }}>
            <span className="brand-mark"><span>о!</span></span>
            <span className="brand-name">островок<span>знаю</span></span>
          </a>
          <div className="topbar-center"><span className="season-dot" /> Путешествуйте увереннее</div>
          <button className="avatar-button" aria-label="Профиль"><span>А</span><ChevronDown size={15} /></button>
        </header>

        <div className="main-layout">
          <aside className="sidebar">
            <div className="side-caption">ВАШЕ ПУТЕШЕСТВИЕ</div>
            <nav className="side-nav" aria-label="Основная навигация">
              <button className={section === 'guest' ? 'nav-item active' : 'nav-item'} onClick={() => { setSection('guest'); setSelected(null) }}><Compass size={18} />Найти отель</button>
              <button className={section === 'hotelier' ? 'nav-item active' : 'nav-item'} onClick={() => { setSection('hotelier'); setSelected(null) }}><MessageCircle size={18} />Для отельера</button>
              <button className={section === 'admin' ? 'nav-item active' : 'nav-item'} onClick={() => { setSection('admin'); setSelected(null) }}><SlidersHorizontal size={18} />Панель команды</button>
            </nav>
            <div className="side-rule" />
            <div className="side-caption">ВАШИ ЗАКЛАДКИ <span className="bookmark-count">{saved.length}</span></div>
            {saved.length ? <div className="saved-list">{saved.map((item) => <button key={item.hotel_id} className="saved-item" onClick={() => openSaved(item)}><Heart size={14} fill="currentColor" /><span>{item.name}{item.city_id !== city && <small> · {cityNames[item.city_id] || item.city_id}</small>}</span></button>)}</div> : <p className="saved-empty">Сохраняйте отели,<br />которые вам понравились</p>}
            <div className="sidebar-bottom"><div className="guide-icon"><CircleHelp size={17} /></div><div><strong>Как это работает</strong><p>Показываем, откуда<br />знаем каждый факт</p></div><ArrowRight size={15} /></div>
          </aside>

          <main className="content">
            {section === 'guest' && <GuestView {...{ city, changeCity, cities, selectedCity, query, setQuery, runSearch, searchState, searchError, searchData, list, visibleResults, remaining, changeFilter, loadMore, openHotel, showMap, setShowMap, savedIds, toggleSave, notify, stay, applyStay, edits, setEdits }} />}
            {section === 'hotelier' && <HotelierView {...{ guestCity: city, cities, activeHotel, setActiveHotel, drafts: hotelierAnswers, setDrafts: setHotelierAnswers, notify, searchData, refreshGuest, guestResults: list.items, showInSearch, tab: hotelierTab, setTab: setHotelierTab }} />}
            {section === 'admin' && <AdminView {...{ guestCity: city, cities, notify }} />}
          </main>
        </div>

        {selected && !booking && <HotelDialog result={selected} ast={selectedAst} data={hotelData} loading={hotelLoading} explanation={explanations[explanationKey]} onClose={() => setSelected(null)} onBook={() => setBooking(true)} onSave={() => toggleSave(selected)} isSaved={savedIds.has(selected.hotel.hotel_id)} onOpenHotelier={(hotelId, tab) => { setActiveHotel(hotelId); setHotelierTab(tab); setSection('hotelier'); setSelected(null) }} />}
        {booking && selected && <BookingDialog result={selected} ast={selectedAst} timeZone={zoneOf(selected.hotel.city_id)} stay={stay} applyStay={applyStay} booked={booked} labels={labels} onClose={() => { setBooking(false); setBooked(false); if (booked) setSelected(null) }} onConfirm={() => setBooked(true)} />}
        <div className="toast-region" role="status" aria-live="polite">{toast && <div className="toast"><CheckCircle2 size={17} aria-hidden="true" />{toast}</div>}</div>
        <footer className="footer"><span>о!знаю</span><span>Факты с источниками. Неизвестное — это неизвестное.</span><a href="https://ostrovok.tech/hackathon/" target="_blank" rel="noreferrer">О! Хакатон 2026 <ExternalLink size={12} /></a></footer>
      </div>
    </LabelsContext.Provider>
  )
}

function GuestView({ city, changeCity, cities, selectedCity, query, setQuery, runSearch, searchState, searchError, searchData, list, visibleResults, remaining, changeFilter, loadMore, openHotel, showMap, setShowMap, savedIds, toggleSave, notify, stay, applyStay, edits, setEdits }) {
  const ast = searchData?.ast
  const [stayDraft, setStayDraft] = useState(stay)
  const [stayOpen, setStayOpen] = useState(false)
  const stayRef = useRef(null)
  useDialog(stayRef, stayOpen, () => setStayOpen(false))
  const [examplesOpen, setExamplesOpen] = useState(false)
  const today = cityToday(city, selectedCity.timezone)
  // Сравнение 2–3 отелей бок о бок по условиям запроса; новый поиск — новое сравнение.
  const [compare, setCompare] = useState([])
  const [compareOpen, setCompareOpen] = useState(false)
  useEffect(() => { setCompare([]); setCompareOpen(false) }, [searchData])
  const comparedIds = useMemo(() => new Set(compare.map((r) => r.hotel.hotel_id)), [compare])
  const toggleCompare = useCallback((result) => {
    setCompare((current) => {
      if (current.some((r) => r.hotel.hotel_id === result.hotel.hotel_id)) return current.filter((r) => r.hotel.hotel_id !== result.hotel.hotel_id)
      if (current.length >= 3) { notify('Сравнить можно до трёх отелей'); return current }
      return [...current, result]
    })
  }, [notify])
  const counts = searchCounts(searchData)
  const notes = guestNotes(searchData?.notes)
  function openStayPicker() {
    setStayDraft(stay)
    setStayOpen(true)
  }
  function submitCurrent() {
    if (!query.trim()) return notify('Опишите, что для вас важно в поездке')
    runSearch(query, city)
  }
  function pickScenario(item) {
    setExamplesOpen(false)
    changeCity(item.city, item.text)
  }
  const stayLabel = `${formatDateRange(stay.checkIn, stay.checkOut)} · ${formatGuests(stay.adults, stay.children)}`
  return (
    <>
      <div className="content-topline"><div className="breadcrumb">Главная <span>/</span> Отели</div><div className="topline-actions"><span className={`data-depth ${selectedCity.depth === 'full' ? 'deep' : ''}`}><i />{selectedCity.depth === 'full' ? 'Глубокие данные' : 'Базовые данные'}</span><div className="stay-picker"><button className="quiet-button stay-trigger" aria-expanded={stayOpen} aria-haspopup="dialog" onClick={() => stayOpen ? setStayOpen(false) : openStayPicker()}><CalendarDays size={15} /> {stayLabel} <ChevronDown size={14} className={stayOpen ? 'rotated' : ''} /></button>{stayOpen && <><button className="stay-backdrop" aria-label="Отменить выбор дат" tabIndex={-1} onClick={() => setStayOpen(false)} /><div ref={stayRef} tabIndex={-1} className="stay-popover" role="dialog" aria-modal="true" aria-label="Даты и гости"><div className="stay-popover-heading"><div><span className="section-kicker">ВАША ПОЕЗДКА</span><strong>Даты и гости</strong></div><button className="close-button" aria-label="Закрыть" onClick={() => setStayOpen(false)}><X size={16} /></button></div><StayFields value={stayDraft} onChange={setStayDraft} today={today} /><p className="stay-api-note">Даты добавятся в запрос, и подборка пересчитается: закат и погода считаются на эти даты. Гости нужны для оформления.</p><button className="primary-button stay-apply" disabled={Boolean(stayError(stayDraft, today))} onClick={() => { setStayOpen(false); applyStay(stayDraft, { always: true }) }}>Применить даты</button></div></>}</div></div></div>
      <section className="hero">
        <div className="hero-orbit orbit-one" /><div className="hero-orbit orbit-two" />
        <div className="hero-copy"><div className="eyebrow"><Sparkles size={14} /> ВАШИ УСЛОВИЯ — НАШИ ОТВЕТЫ</div><h1>Найдём место,<br />которое <em>подойдёт.</em></h1><p>Расскажите, каким вы представляете отдых.<br />Мы покажем, что знаем о каждом отеле.</p></div>
        <div className="hero-stamp"><div className="stamp-ring"><span>О!</span><small>ЗНАЮ</small></div><div className="stamp-caption">БОЛЬШЕ, ЧЕМ<br />ЕСТЬ В КАРТОЧКЕ</div></div>
        <div className="search-panel">
          <div className="search-location"><MapPin size={18} /><div className="field-stack"><span>КУДА</span><select aria-label="Город" value={city} onChange={(e) => changeCity(e.target.value)}>{(cities.length ? cities : Object.entries(cityNames).map(([city_id, name]) => ({ city_id, name }))).map((item) => <option key={item.city_id} value={item.city_id}>{item.name}</option>)}</select></div><ChevronDown size={15} /></div>
          <div className="search-divider" />
          <div className="query-box"><label htmlFor="request">ВАШИ ПОЖЕЛАНИЯ</label><textarea id="request" rows="2" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitCurrent() } }} placeholder="Например, тихий номер с видом на закат…" /></div>
          <button className="search-submit" disabled={searchState === 'loading' || !query.trim()} onClick={submitCurrent}><Search size={17} />{searchState === 'loading' ? 'Ищем…' : 'Найти отель'}</button>
        </div>
      </section>

      <div className="scenario-strip"><button className="scenario-toggle" aria-expanded={examplesOpen} onClick={() => setExamplesOpen(!examplesOpen)}><Sparkles size={14} />Примеры запросов <span>7 сценариев + сезонный бонус</span><ChevronDown size={14} className={examplesOpen ? 'rotated' : ''} /></button>{examplesOpen && <div className="scenario-options">{scenarioPresets.map((item) => <button key={item.label} onClick={() => pickScenario(item)}><span>{item.label}</span><small>{cityNames[item.city]}</small></button>)}</div>}</div>

      <div className="results-toolbar"><div><span className="section-kicker">ПОДБОРКА ДЛЯ ВАС</span><h2>{searchState === 'ready' ? `Найдено вариантов: ${filterTotal(list.status, searchData)}` : 'Отели под ваш запрос'} <span className="city-tag">{selectedCity.name}</span></h2></div><div className="toolbar-actions"><div className="status-filter" role="group" aria-label="Статус">{FILTERS.map(([value, label]) => <button key={value} className={`filter-pill ${list.status === value ? 'active' : ''}`} aria-pressed={list.status === value} disabled={!searchData} onClick={() => changeFilter(value)}>{label}{searchData && <b>{value === 'all' ? filterTotal('all', searchData) : counts[value]}</b>}</button>)}</div><button className="view-toggle" onClick={() => setShowMap(!showMap)}>{showMap ? 'Скрыть карту' : 'Показать карту'}<MapPin size={15} /></button></div></div>

      {searchState === 'offline' && <div className="notice notice-warn"><CircleHelp size={18} /><div><strong>Не удалось подключиться к поиску</strong><span>Запустите приложение вместе с API на порту 8080. Интерфейс отправляет запрос на <code>POST /api/search</code>.</span></div></div>}
      {searchState === 'error' && <div className="notice notice-warn"><CircleHelp size={18} /><div><strong>Поиск не сработал</strong><span>{searchError}</span></div></div>}
      {notes.length > 0 && <div className="notice notice-info"><Sparkles size={17} /><div><strong>Как сформирована подборка</strong>{notes.map((note, index) => <span key={index}>{note}</span>)}</div></div>}

      {ast && <UnderstoodCard {...{ ast, query, city, runSearch, edits, setEdits, notify, openStayPicker, searching: searchState === 'loading' }} />}

      <div className={`result-layout ${showMap ? '' : 'no-map'}`}>
        <div className="hotel-list">
          {searchState === 'loading' && <div className="loading-block" aria-label="Ищем"><div className="skeleton-line" /><div className="skeleton-line short" /><div className="skeleton-card" /></div>}
          {searchState === 'offline' && <div className="empty-state offline-results"><div className="offline-illustration"><Compass size={25} /><span /></div><span className="section-kicker">ПОДБОРКА ПОЯВИТСЯ ЗДЕСЬ</span><h3>Отели ждут подключения API</h3><p>Карта показывает город. Чтобы сравнить отели по вашим условиям, запустите сервер поиска.</p><div className="offline-steps"><span><i>1</i> Запустите API на порту 8080</span><span><i>2</i> Нажмите «Найти отель»</span></div></div>}
          {searchState === 'ready' && !visibleResults.length && !list.loading && <div className="empty-state"><Compass size={30} /><h3>{filterTotal('all', searchData) ? 'Нет вариантов с таким статусом' : 'В этом городе пока нет отелей в выборке'}</h3><p>{filterTotal('all', searchData) ? 'Выберите другой фильтр или посмотрите все варианты.' : 'Попробуйте другой город или измените запрос.'}</p></div>}
          {visibleResults.map((result, index) => <HotelCard key={result.hotel.hotel_id} result={result} index={index} onOpen={openHotel} isSaved={savedIds.has(result.hotel.hotel_id)} onToggleSave={toggleSave} isCompared={comparedIds.has(result.hotel.hotel_id)} onToggleCompare={toggleCompare} />)}
          {list.loading && <div className="loading-inline">Загружаем варианты…</div>}
          {searchState === 'ready' && remaining > 0 && <button className="load-more" disabled={list.loading} onClick={loadMore}>Показать ещё <span>осталось {remaining}</span></button>}
          {searchState === 'idle' && <div className="empty-state"><Search size={28} /><h3>Опишите свою поездку</h3><p>Найдём варианты и объясним каждый ответ.</p></div>}
          {searchData && <div className="source-note"><ShieldCheck size={15} /><span>Оценки не закрывают обязательные условия. Неизвестное не считается отрицательным ответом.</span></div>}
        </div>
        {showMap && <HotelMap results={visibleResults} city={city} onSelect={openHotel} />}
      </div>
      {compare.length > 0 && <div className="compare-bar" role="region" aria-label="Сравнение отелей"><Columns3 size={16} /><span>Сравнение: {compare.map((r) => r.hotel.name).join(' · ')}</span><button className="primary-button" disabled={compare.length < 2} onClick={() => setCompareOpen(true)}>{compare.length < 2 ? 'Отметьте ещё отель' : 'Сравнить'}</button><button className="text-button" onClick={() => setCompare([])}>Очистить</button></div>}
      {compareOpen && compare.length > 1 && <CompareDialog results={compare} onClose={() => setCompareOpen(false)} onOpen={(r) => { setCompareOpen(false); openHotel(r) }} />}
    </>
  )
}

function CompareDialog({ results, onClose, onOpen }) {
  const dialogRef = useRef(null)
  useDialog(dialogRef, true, onClose)
  const labels = useLabels()
  const rows = compareRows(results, labels)
  const yes = results.map((_, column) => rows.filter((row) => row.cells[column]?.kind === 'yes').length)
  return <div className="modal-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}><section ref={dialogRef} tabIndex={-1} className="hotel-dialog compare-dialog" role="dialog" aria-modal="true" aria-label="Сравнение отелей">
    <div className="dialog-top"><div><span className="section-kicker">СРАВНЕНИЕ ПО ВАШЕМУ ЗАПРОСУ</span><button className="close-button" onClick={onClose} aria-label="Закрыть"><X size={19} /></button></div></div>
    <div className="compare-scroll"><table className="compare-table">
      <thead><tr><th scope="col">Условие</th>{results.map((r, i) => <th scope="col" key={r.hotel.hotel_id}><button className="text-button" onClick={() => onOpen(r)}>{r.hotel.name}</button><span className={`mini-status ${verdictKind(r.status)}`}>{r.status_label || STATUS_LABELS[r.status]}</span><small>да — {yes[i]} из {rows.length}</small></th>)}</tr></thead>
      <tbody>{rows.map((row) => <tr key={row.key}><th scope="row"><span className={`condition-dot ${row.required ? 'required' : ''}`} />{row.title}</th>{row.cells.map((cell, i) => <td key={i} className={cell ? `cell-${cell.kind}` : 'cell-none'}>{cell ? <><VerdictIcon value={cell.kind} /> <span>{cell.text}</span></> : '—'}</td>)}</tr>)}</tbody>
    </table></div>
  </section></div>
}

function UnderstoodCard({ ast, query, city, runSearch, edits, setEdits, notify, openStayPicker, searching }) {
  const labels = useLabels()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null)
  const [draftEdits, setDraftEdits] = useState([])
  const [answers, setAnswers] = useState({})
  const [closedClarify, setClosedClarify] = useState({})
  useEffect(() => { setEditing(false); setDraft(null); setDraftEdits([]) }, [ast])
  const shown = editing && draft ? draft : ast
  const chips = useMemo(() => {
    const items = conditionChips(shown)
    const counts = new Map()
    for (const chip of items) counts.set(chip.label, (counts.get(chip.label) || 0) + 1)
    return items.map((chip) => ({ ...chip, repeated: counts.get(chip.label) > 1 }))
  }, [shown])
  const outOfCatalog = shown.out_of_catalog || []
  const required = chips.filter((chip) => chip.kind === 'required')
  const preferred = chips.filter((chip) => chip.kind === 'preferred')
  const oocRequired = outOfCatalog.filter((item) => item.requiredness === 'required')
  const oocDesired = outOfCatalog.filter((item) => item.requiredness === 'desired')
  const oocOther = outOfCatalog.filter((item) => item.requiredness !== 'required' && item.requiredness !== 'desired')
  const unparsed = shown.unparsed_spans || []
  const hasConditions = (ast.hard?.length || 0) + (ast.soft?.length || 0) > 0
  // Заметка о правках — только когда правка гостя действует в этом разборе.
  const editsHere = edits.some((edit) => (ast[edit.to] || []).some((item) => item.feature_id === edit.feature_id && (item.text_span || '') === edit.text_span))
  const tripDates = ast.dates?.from ? formatDateRange(ast.dates.from, ast.dates.to || addIsoDays(ast.dates.from, 1), true) : ''
  const tripGuests = ast.guests?.adults ? formatGuests(ast.guests.adults, ast.guests.children_ages?.length || 0) : ''
  const tripArea = areaText(ast, labels)

  function startEditing() {
    setDraft(ast)
    setDraftEdits([])
    setEditing(true)
  }
  function move(chip) {
    if (chip.block) return notify(chip.block)
    const condition = chip.conditions[0]
    const next = moveCondition(draft, condition.atom_id)
    if (!next) return notify('Это условие перенести нельзя — поправьте текст запроса')
    setDraft(next)
    setDraftEdits((items) => recordEdit(items, editOf(condition, chip.kind === 'required' ? 'soft' : 'hard')))
  }
  function done() {
    setEditing(false)
    if (!draftEdits.length) return
    setEdits((items) => draftEdits.reduce(recordEdit, items))
    runSearch(ast.raw_text || query, city, { ast: draft })
  }
  function resetEdits() {
    setEdits([])
    runSearch(query, city)
  }
  function choose(item, option) {
    const refined = refineQuery(ast.raw_text || query, item, option)
    setAnswers((items) => ({ ...items, [clarifyKey(item)]: { option, note: refined.note } }))
    if (refined.text) runSearch(refined.text, city)
  }

  return <section className="understood-card" aria-labelledby="understood-title">
    <div className="understood-heading"><div className="understood-icon"><Sparkles size={16} /></div><div><span className="section-kicker">РАЗБОР ЗАПРОСА · {ast.parsed_by === 'llm' ? 'МОДЕЛЬ' : ast.parsed_by === 'rules' ? 'ПРАВИЛА' : 'ПРИМЕР'}</span><h3 id="understood-title">Я понял так</h3></div>
      {hasConditions && (editing
        ? <div className="edit-actions"><button className="text-button" onClick={() => setEditing(false)}>Отменить</button><button className="text-button strong" onClick={done}>Готово <Check size={14} /></button></div>
        : <button className="text-button" onClick={startEditing}>Изменить <ArrowRight size={14} /></button>)}
    </div>
    {editing && <p className="edit-hint">Нажмите на условие, чтобы перенести его между «обязательно» и «желательно». Подборка пересчитается по «Готово».</p>}
    {!editing && editsHere && <p className="edit-hint">Ваши правки разбора сохраняются для следующих поисков. <button className="small-link" onClick={resetEdits}><RotateCcw size={12} /> Сбросить правки</button></p>}
    {(tripDates || tripGuests || tripArea) && <div className="trip-line"><CalendarDays size={14} /><span>Из запроса: {[tripArea, tripDates, tripGuests].filter(Boolean).join(' · ')}</span></div>}
    <div className="condition-groups">
      <div className="condition-group"><div className="group-label"><span className="condition-dot required" />ОБЯЗАТЕЛЬНО</div>{required.map((chip) => <ConditionChip key={chip.key} chip={chip} editing={editing} onMove={move} />)}{oocRequired.map((item, index) => <OutOfCatalogChip key={`r${item.atom_id || index}`} item={item} />)}{!required.length && !oocRequired.length && <span className="muted-small">Пока не указано</span>}</div>
      <div className="condition-group"><div className="group-label"><span className="condition-dot preferred" />ЖЕЛАТЕЛЬНО</div>{preferred.map((chip) => <ConditionChip key={chip.key} chip={chip} editing={editing} onMove={move} />)}{oocDesired.map((item, index) => <OutOfCatalogChip key={`d${item.atom_id || index}`} item={item} />)}{!preferred.length && !oocDesired.length && <span className="muted-small">Пока не указано</span>}</div>
      {oocOther.map((item, index) => <div className="unknown-box" key={`o${item.atom_id || index}`}><span className="unknown-symbol">?</span><div><strong>{labels.out_of_catalog || 'Такое мы пока не знаем'}</strong><span>{item.text_span}</span></div></div>)}
      {outOfCatalog.some((item) => !outOfCatalogCheck(item).checked) && <p className="ooc-note">«Не проверяем» — такого пока нет в нашем каталоге. Обязательная часть оставляет отель не выше «Нужно уточнить». Мы записали, что такое спрашивают.</p>}
      {unparsed.length > 0 && <div className="unknown-box unparsed-box"><span className="unknown-symbol">?</span><div><strong>Не учли в поиске</strong><span>{unparsed.map((span) => `«${span.text}»`).join(', ')} — эту часть мы не проверяли, на статусы она не влияет. Переформулируйте, если она важна.</span></div></div>}
      {(shown.clarify || []).map((item) => {
        const key = clarifyKey(item)
        const kind = clarifyKind(item)
        const answer = answers[key]
        const open = !closedClarify[key]
        return <div className={`clarify-box ${answer ? 'answered' : ''}`} key={key}>
          <button className="clarify-head" aria-expanded={open} onClick={() => setClosedClarify((items) => ({ ...items, [key]: open }))}><CircleHelp size={16} /><span>Уточнить: {item.about}</span><ChevronDown size={15} className={open ? 'rotated' : ''} /></button>
          {open && <div className="clarify-options"><p>{item.question}</p>
            {kind === 'dates' && <div><button className="option-chip" onClick={openStayPicker}><CalendarDays size={13} /> Выбрать даты поездки</button></div>}
            {item.options?.length > 0 && <div>{item.options.map((option) => <button key={option} className={`option-chip ${answer?.option === option ? 'selected' : ''}`} disabled={searching} onClick={() => choose(item, option)}>{option}</button>)}</div>}
            <small className="clarify-note">{answer?.note || (kind === 'dates' ? 'Даты добавятся в запрос, и подборка пересчитается.' : kind === 'open' ? 'Ответьте в тексте запроса — разбор это учтёт.' : 'Выбор уточнит запрос и пересчитает подборку.')}</small>
          </div>}
        </div>
      })}
    </div>
  </section>
}

function conditionDetail(condition) {
  const parts = []
  const period = conditionPeriod(condition.params)
  if (period) parts.push(period)
  const value = Number(condition.value)
  if (typeof condition.value === 'number' || (typeof condition.value === 'string' && condition.value.trim() !== '' && Number.isFinite(value))) {
    const field = condition.field || ''
    const unit = /(^|_)m$|distance/.test(field) ? ' м' : /_mm$/.test(field) ? ' мм' : /_C$/.test(field) ? ' °C' : /_kg$/.test(field) ? ' кг' : /dBA$/.test(field) ? ' дБ' : /hours/.test(field) ? ' ч' : ''
    const op = { lte: 'до', lt: 'меньше', gte: 'от', gt: 'больше', eq: '' }[condition.op]
    if (op !== undefined) parts.push(`${op ? `${op} ` : ''}${formatNumber(value)}${unit}`)
  }
  return parts.join(' · ')
}

function ConditionChip({ chip, editing, onMove }) {
  const labels = useLabels()
  const condition = chip.conditions[0]
  const name = chip.label || featureName(condition.feature_id, labels)
  const detail = chip.group === 'OR' ? `один из ${chip.variants} вариантов` : [chip.repeated ? featureName(condition.feature_id, labels) : '', conditionDetail(condition)].filter(Boolean).join(' · ')
  const locked = editing && Boolean(chip.block)
  const title = editing ? chip.block || `Перенести в «${chip.kind === 'required' ? 'желательно' : 'обязательно'}»` : featureName(condition.feature_id, labels)
  return <button type="button" className={`condition-chip ${condition.provenance === 'inferred' ? 'inferred' : ''} ${editing ? (locked ? 'locked' : 'editable') : ''}`} title={title} aria-disabled={locked || undefined} onClick={editing ? () => onMove(chip) : undefined}>{name}{detail && <small>{detail}</small>}{editing && (locked ? <Lock size={11} className="edit-arrow" aria-hidden="true" /> : <span className="edit-arrow" aria-hidden="true">↔</span>)}</button>
}

function OutOfCatalogChip({ item }) {
  const check = outOfCatalogCheck(item)
  return <span className={`condition-chip ooc-chip${check.checked ? ' checked' : ''}`} title={check.checked ? check.note : 'Такое мы пока не знаем'}>{item.text_span}<small>{check.note}</small></span>
}

function GuestCounter({ label, value, min, max, onChange }) {
  return <div className="guest-counter"><span>{label}</span><div><button aria-label={`Уменьшить: ${label}`} disabled={value <= min} onClick={() => onChange(Math.max(min, value - 1))}><Minus size={14} /></button><strong>{value}</strong><button aria-label={`Добавить: ${label}`} disabled={value >= max} onClick={() => onChange(Math.min(max, value + 1))}><Plus size={14} /></button></div></div>
}

function StayFields({ value, onChange, today }) {
  const error = stayError(value, today)
  return <>
    <div className="stay-dates">
      <label>Заезд<input type="date" min={today} value={value.checkIn} onChange={(e) => onChange({ ...value, checkIn: e.target.value, checkOut: value.checkOut <= e.target.value ? addIsoDays(e.target.value, 1) : value.checkOut })} /></label>
      <label>Выезд<input type="date" min={addIsoDays(value.checkIn || today, 1)} value={value.checkOut} onChange={(e) => onChange({ ...value, checkOut: e.target.value })} /></label>
    </div>
    <div className="stay-guests"><span>Гости</span><GuestCounter label="Взрослые" value={value.adults} min={1} max={8} onChange={(adults) => onChange({ ...value, adults })} /><GuestCounter label="Дети" value={value.children} min={0} max={6} onChange={(children) => onChange({ ...value, children })} /></div>
    {error && <p className="stay-error" role="alert">{error}</p>}
  </>
}

function ReasonRow({ item, title }) {
  const kind = verdictKind(item.verdict)
  return <div className={`reason-row ${item.required ? 'required' : 'preferred'}`}><span className={`reason-marker ${kind} ${item.required ? '' : 'soft'}`} /><span className="reason-title">{title}<em className="req-tag">{item.required ? 'обязательно' : 'желательно'}</em></span><b>{verdictText(item)}<Marks item={item} /></b></div>
}

const HotelCard = memo(function HotelCard({ result, index, onOpen, isSaved, onToggleSave, isCompared, onToggleCompare }) {
  const labels = useLabels()
  const hotel = result.hotel
  const items = orderedItems(result)
  const titles = distinctTitles(items, labels)
  const unknownCount = (result.hard || []).filter((item) => verdictKind(item.verdict) === 'unknown').length
  const bestCategory = (result.categories || []).find((item) => item.room_type_id === result.best_category)
  const details = items.slice(0, 4)
  const statusClass = { yes: 'fits', no: 'not-fit', unknown: 'needs-check' }[verdictKind(result.status)]
  return <article className="hotel-card">
    <button className={`save-button ${isSaved ? 'saved' : ''}`} onClick={(e) => { e.stopPropagation(); onToggleSave(result) }} aria-label={isSaved ? 'Убрать из закладок' : 'Сохранить в закладки'} aria-pressed={isSaved}><Heart size={17} fill={isSaved ? 'currentColor' : 'none'} /></button>
    {onToggleCompare && <button className={`compare-toggle ${isCompared ? 'on' : ''}`} onClick={(e) => { e.stopPropagation(); onToggleCompare(result) }} aria-pressed={isCompared} aria-label={isCompared ? 'Убрать из сравнения' : 'Добавить к сравнению'}><Columns3 size={14} />{isCompared ? 'В сравнении' : 'Сравнить'}</button>}
    <button className="hotel-card-main" onClick={() => onOpen(result)}>
      <div className={`hotel-art art-${index % 4}`}><div className="art-sun" /><div className="art-water" /><div className="art-palm">✳</div><span className="art-label">{cityNames[hotel.city_id] || 'ОТЕЛЬ'}</span></div>
      <div className="hotel-details"><div className="hotel-meta"><span>{hotelKind(hotel.hotel_kind).toUpperCase()}</span>{hotel.stars > 0 && <><span className="meta-dot" /><span>{hotel.stars}★</span></>}<span className="meta-dot" /><span>{hotel.subzone || hotel.address?.split(',').slice(-1)[0]?.trim() || cityNames[hotel.city_id]}</span></div><h3>{hotel.name}</h3><div className={`fit-status ${statusClass}`}><span className="status-icon"><VerdictIcon value={result.status} /></span>{result.status_label || STATUS_LABELS[result.status] || 'Нужно уточнить'}{result.status === 'needs_check' && unknownCount > 0 && <span className="status-extra">· {unknownCount} неизвестно</span>}</div>
        {result.explanation?.text && <span className="hotel-explanation">{result.explanation.text}</span>}
        {bestCategory && <div className="best-category"><BedDouble size={15} /><span><small>Лучшая категория по условиям</small><strong>{bestCategory.name || bestCategory.room_type_id}</strong></span><span className={`mini-status ${verdictKind(bestCategory.status)}`}>{bestCategory.status_label || STATUS_LABELS[bestCategory.status] || 'Уточнить'}</span></div>}
        <div className="hotel-reasons">{details.map((item, i) => <ReasonRow key={`${item.atom_id || item.feature_id}-${i}`} item={item} title={titles[i]} />)}{items.length > details.length && <span className="reasons-more">и ещё {items.length - details.length} — в «Подробнее»</span>}</div>
        <div className="hotel-card-bottom"><span className="verify-label"><ShieldCheck size={14} /> Источники у каждого факта</span><span className="details-link">Подробнее <ArrowRight size={15} /></span></div></div>
    </button>
  </article>
})

function HotelMap({ results, city, onSelect }) {
  const mapRef = useRef(null)
  const mapInstance = useRef(null)
  const onSelectRef = useRef(onSelect)
  const fitKey = useRef('')
  const [layersOpen, setLayersOpen] = useState(false)
  const [showHotels, setShowHotels] = useState(true)
  onSelectRef.current = onSelect
  useEffect(() => {
    if (!mapRef.current || mapInstance.current) return
    const [center, zoom] = CITY_VIEWS[city] || CITY_VIEWS.sochi
    const map = L.map(mapRef.current, { zoomControl: false, scrollWheelZoom: false }).setView(center, zoom)
    L.control.zoom({ position: 'bottomright' }).addTo(map)
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>', maxZoom: 19 }).addTo(map)
    mapInstance.current = map
    fitKey.current = ''
    return () => { map.remove(); mapInstance.current = null }
  }, [city])
  useEffect(() => {
    const map = mapInstance.current
    if (!map) return
    const markers = L.layerGroup().addTo(map)
    const points = results.flatMap((result, index) => {
      const { latitude, longitude, name } = result.hotel
      return Number.isFinite(latitude) && Number.isFinite(longitude) ? [{ result, index, name, coords: [latitude, longitude] }] : []
    })
    function drawMarkers() {
      markers.clearLayers()
      if (!showHotels) return
      const zoom = map.getZoom()
      const projected = points.map((point) => ({ ...point, ...map.project(point.coords, zoom) }))
      // На максимальном приближении в группе только отели с одинаковыми координатами.
      for (const group of groupMapPoints(projected, zoom >= 18 ? 0 : 44)) {
        const clustered = group.length > 1
        const bounds = L.latLngBounds(group.map((point) => point.coords))
        const icon = L.divIcon({ className: 'map-marker-wrap', html: clustered ? `<span class="map-cluster">${group.length}</span>` : `<span class="map-marker ${group[0].index === 0 ? 'primary' : ''}"><i>${group[0].index + 1}</i></span>`, iconSize: clustered ? [44, 44] : [34, 42], iconAnchor: clustered ? [22, 22] : [17, 40] })
        const title = document.createElement('span')
        title.textContent = clustered ? `Отелей в этой области: ${group.length}` : group[0].name
        const marker = L.marker(bounds.getCenter(), { icon, title: title.textContent }).addTo(markers).bindTooltip(title, { direction: 'top', offset: [0, -24] })
        marker.on('click', () => {
          if (!clustered) return onSelectRef.current(group[0].result)
          if (map.getZoom() < 18) {
            map.fitBounds(bounds, { padding: [50, 50], maxZoom: Math.min(map.getZoom() + 2, 18) })
            return
          }
          const list = document.createElement('div')
          list.className = 'map-cluster-list'
          for (const point of group) {
            const button = document.createElement('button')
            button.textContent = point.name
            button.addEventListener('click', () => onSelectRef.current(point.result))
            list.append(button)
          }
          marker.bindPopup(list, { maxWidth: 260 }).openPopup()
        })
      }
    }
    map.on('zoomend', drawMarkers)
    drawMarkers()
    // Масштаб подгоняем только под новый набор отелей, а не на каждый рендер.
    const key = points.map((point) => point.result.hotel.hotel_id).join(',')
    if (showHotels && key && key !== fitKey.current) {
      fitKey.current = key
      if (points.length > 1) map.fitBounds(points.map((point) => point.coords), { padding: [40, 40], maxZoom: 12 })
      else map.setView(points[0].coords, 13)
    }
    return () => { map.off('zoomend', drawMarkers); map.removeLayer(markers) }
  }, [results, city, showHotels])
  return <aside className="map-panel"><div className="map-head"><div><span className="section-kicker">НА КАРТЕ</span><h3>{cityNames[city] || 'Город'}</h3></div><div className="map-layer-wrap"><button className={`map-layer-button ${layersOpen ? 'active' : ''}`} aria-label="Слои карты" aria-expanded={layersOpen} aria-haspopup="menu" onClick={() => setLayersOpen((open) => !open)}><SlidersHorizontal size={16} /></button>{layersOpen && <div className="map-layer-menu" role="menu" aria-label="Слои карты"><span className="layer-menu-title">Слои карты</span><button className={`layer-option ${showHotels ? 'enabled' : ''}`} role="menuitemcheckbox" aria-checked={showHotels} onClick={() => setShowHotels((enabled) => !enabled)}><i className="legend-hotel" /><span><strong>Отели из подборки</strong><small>{showHotels ? 'Маркеры показаны' : 'Маркеры скрыты'}</small></span><span className={`layer-switch ${showHotels ? 'on' : ''}`} /></button></div>}</div></div><div className="map-container" ref={mapRef}><div className="map-legend">{showHotels ? <span><i className="legend-hotel" />Отели на этой странице подборки</span> : <span className="legend-hidden">Маркеры отелей скрыты</span>}</div></div><div className="map-foot"><MapPin size={15} /><span>Маркеры — отели этой страницы подборки по координатам из каталога; расстояния до мест и маршруты — в карточке отеля. Карта © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">участники OpenStreetMap</a>.</span></div></aside>
}

function ConditionRow({ item, required, ast, ask }) {
  const labels = useLabels()
  const kind = verdictKind(item.verdict)
  const period = conditionPeriod(conditionParams(ast, item.atom_id))
  const subtitle = itemSubtitle(item, labels)
  const fallback = kind === 'yes' ? (hasEstimate(item) ? 'Похоже, выполнено — это оценка' : 'Условие выполнено по данным') : kind === 'no' ? 'Условие не выполнено' : 'Нужно уточнить'
  return <div className={`fact-check-row ${required ? '' : 'preferred'}`}>
    <span className={`check-disc ${kind} ${required ? '' : 'soft'}`}><VerdictIcon value={item.verdict} /></span>
    <div className="condition-copy"><strong>{itemTitle(item, labels)}{period && <small className="period-tag">{period}</small>}</strong>
      {subtitle && <small className="item-subtitle">{subtitle}</small>}
      <p className="condition-reason">{item.reason || fallback}</p>
      {item.items?.length > 0 && <ul className="variant-list">{item.items.map((child, index) => <li key={`${child.atom_id || child.feature_id}-${index}`} className={verdictKind(child.verdict)}><VerdictIcon value={child.verdict} size={11} />{child.feature_name || itemTitle(child, labels)} — {shortVerdict(child)}</li>)}</ul>}
      {item.windows?.count > 0 && <small className="item-subtitle">Окон в расчёте: {item.windows.count}</small>}
      <Marks item={item} />
      {kind === 'unknown' && ask && <AskHotel item={item} ask={ask} />}
    </div>
    <span className={`mini-status ${kind}`}>{kind === 'yes' ? 'Да' : kind === 'no' ? 'Нет' : 'Уточнить'}</span>
  </div>
}

// «Нужно уточнить» → вопрос отелю одной кнопкой (D5): по характеристике отеля — готовый вопрос с
// предзаполнением; где окна номеров (вид, закат, солнце) — вкладка «Где окна номеров» кабинета.
function AskHotel({ item, ask }) {
  const question = ask.questions.get(item.feature_id)
  if (item.windows) return <button className="inline-action ask-hotel" onClick={() => ask.open('placement')}>Спросить отель, где окна номеров <ArrowRight size={13} /></button>
  if (!question || question.answer_status === 'answered') return null
  if (question.answer_status === 'asked') return <span className="asked-note"><Check size={13} /> Вопрос отправлен отелю — ответ придёт в кабинет отельера <button className="inline-action" onClick={() => ask.open('questions')}>Открыть кабинет</button></span>
  return <button className="inline-action ask-hotel" disabled={ask.busy === question.question_id} onClick={() => ask.send(question)}>{ask.busy === question.question_id ? 'Отправляем…' : 'Спросить отель'} <ArrowRight size={13} /></button>
}

function HotelDialog({ result, ast, data, loading, explanation, onClose, onBook, onSave, isSaved, onOpenHotelier }) {
  const dialogRef = useRef(null)
  useDialog(dialogRef, true, onClose)
  const [allFacts, setAllFacts] = useState(false)
  const hotel = result.hotel
  const hard = result.hard || []
  const soft = result.soft || []
  const relevantIds = useMemo(() => new Set([...hard, ...soft].flatMap((item) => [...(item.fact_ids || []), ...(item.items || []).flatMap((child) => child.fact_ids || [])])), [result])
  const rows = useMemo(() => factRows(data?.facts, relevantIds), [data, relevantIds])
  const statusClass = { yes: 'fits', no: 'not-fit', unknown: 'needs-check' }[verdictKind(result.status)]
  const text = result.explanation?.text || explanation?.text
  const [questions, setQuestions] = useState(new Map())
  const [busy, setBusy] = useState(null)
  useEffect(() => {
    let cancelled = false
    api(`/hotelier/${hotel.hotel_id}/questions`).then((d) => { if (!cancelled) setQuestions(new Map((d.questions || []).map((q) => [q.feature_id, q]))) }).catch(() => {})
    return () => { cancelled = true }
  }, [hotel.hotel_id])
  const ask = {
    questions,
    busy,
    open: (tab) => onOpenHotelier?.(hotel.hotel_id, tab),
    send: async (question) => {
      setBusy(question.question_id)
      try {
        const sent = await api(`/hotelier/${question.hotel_id}/questions/${question.question_id}/ask`, { method: 'POST' })
        setQuestions((current) => new Map(current).set(question.feature_id, { ...question, ...(sent?.question || {}), answer_status: 'asked' }))
      } catch { /* сеть — кнопка останется, можно повторить */ } finally { setBusy(null) }
    },
  }
  return <div className="modal-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}><section ref={dialogRef} tabIndex={-1} className="hotel-dialog" role="dialog" aria-modal="true" aria-label={`Карточка ${hotel.name}`}>
    <div className="dialog-top"><div><span className="section-kicker">{hotelKind(hotel.hotel_kind).toUpperCase()} · {cityNames[hotel.city_id]}</span><button className="close-button" onClick={onClose} aria-label="Закрыть"><X size={19} /></button></div></div>
    <div className="dialog-art art-0"><div className="art-sun" /><div className="art-water" /><span className="art-label">{cityNames[hotel.city_id]}</span></div>
    <div className="dialog-title"><div><h2>{hotel.name}</h2><p><MapPin size={14} />{hotel.address}</p></div><button className={`save-button dialog-save ${isSaved ? 'saved' : ''}`} onClick={onSave} aria-label={isSaved ? 'Убрать из закладок' : 'Сохранить в закладки'} aria-pressed={isSaved}><Heart size={17} fill={isSaved ? 'currentColor' : 'none'} /></button></div>
    <div className={`fit-status ${statusClass}`}><span className="status-icon"><VerdictIcon value={result.status} /></span>{result.status_label || STATUS_LABELS[result.status]}</div>
    {(text || explanation?.state === 'loading') && <p className="dialog-explanation">{text || 'Готовим объяснение…'}</p>}
    {result.stale && <p className="stale-note">Подборка обновилась, а этого отеля нет на загруженной странице: условия ниже — по прежнему запросу.</p>}
    <div className="dialog-section"><div className="dialog-section-heading"><div><span className="section-kicker">ПРОВЕРКА УСЛОВИЙ</span><h3>Что мы знаем</h3></div><span className="fact-count">{hard.length + soft.length} {plural(hard.length + soft.length, 'пункт', 'пункта', 'пунктов')}</span></div>
      {hard.length > 0 && <h4 className="condition-group-title"><span className="condition-dot required" />Обязательные условия</h4>}
      {hard.map((item, index) => <ConditionRow key={`h-${item.atom_id || item.feature_id}-${index}`} item={item} required ast={ast} ask={ask} />)}
      {soft.length > 0 && <h4 className="condition-group-title"><span className="condition-dot preferred" />Желательные — влияют только на порядок</h4>}
      {soft.map((item, index) => <ConditionRow key={`s-${item.atom_id || item.feature_id}-${index}`} item={item} required={false} ast={ast} ask={ask} />)}
      {!hard.length && !soft.length && <div className="empty-facts">В запросе нет условий, которые мы умеем проверять.</div>}
    </div>
    <CategorySection roomTypes={data?.room_types || []} categories={result.categories || []} bestCategory={result.best_category} loading={loading} />
    <div className="dialog-section"><div className="dialog-section-heading"><div><span className="section-kicker">ПАСПОРТ ФАКТОВ</span><h3>Откуда знаем</h3></div><ShieldCheck size={19} className="muted-icon" /></div>
      {loading && <div className="loading-inline">Загружаем факты и источники…</div>}
      {!loading && (allFacts ? rows : rows.slice(0, 14)).map((row) => row.kind === 'windows' ? <WindowsRow key={row.key} row={row} /> : <FactRow key={row.key} fact={row.fact} hotelId={hotel.hotel_id} />)}
      {!loading && rows.length > 14 && <button className="inline-action" onClick={() => setAllFacts(!allFacts)}>{allFacts ? 'Свернуть' : `Показать все (${rows.length})`}</button>}
      {!loading && !rows.length && <div className="empty-facts">Фактов об этом отеле у нас пока нет.</div>}
    </div>
    <div className="dialog-actions"><a className="source-link" href={hotel.hotel_url} target="_blank" rel="noreferrer">Страница Островка <ExternalLink size={14} /></a><button className="primary-button" onClick={onBook}>К оформлению <ArrowRight size={16} /></button></div>
  </section></div>
}

function CategorySection({ roomTypes, categories, bestCategory, loading }) {
  if (loading && !roomTypes.length) return null
  const evaluated = new Map(categories.map((item) => [item.room_type_id, item]))
  const items = roomTypes.length ? roomTypes : categories
  return <div className="dialog-section category-section"><div className="dialog-section-heading"><div><span className="section-kicker">КАТЕГОРИИ НОМЕРОВ</span><h3>Какой номер подходит</h3></div><BedDouble size={19} className="muted-icon" /></div>
    {!items.length && <div className="empty-facts">На странице отеля в Островке категорий номеров нет — условия проверены по отелю и корпусам.</div>}
    {items.map((room) => {
      const category = evaluated.get(room.room_type_id) || room
      const placements = category.placements?.length ? category.placements : room.placements || []
      const floors = placementFloors(placements)
      const status = category.status
      const guarantee = room.placement_set?.guarantee
      const coverage = room.placement_set?.completeness
      const placementSummary = coverage === 'complete' ? 'Список размещений полный' : coverage === 'partial' ? 'Список размещений неполный' : placements.length ? 'Есть данные о размещениях' : null
      return <div className={`category-row ${room.room_type_id === bestCategory ? 'best' : ''}`} key={room.room_type_id}>
        <div className="category-row-heading"><div><strong>{room.name || room.room_type_id}</strong>{room.room_type_id === bestCategory && <span className="best-category-tag">Лучший вариант</span>}</div>{status && <span className={`mini-status ${verdictKind(status)}`}>{category.status_label || STATUS_LABELS[status] || 'Нужно уточнить'}</span>}</div>
        {(placementSummary || guarantee) && <span className="category-meta">{placementSummary}{placementSummary && guarantee && ' · '}{guarantee ? ({ all_guaranteed: 'все номера гарантированы', exact_room: 'номер выбран точно', some_possible: 'есть подходящие номера', on_request: 'по запросу' }[guarantee] || guarantee) : ''}</span>}
        {floors.length > 0 && <div className="placement-list">{floors.slice(0, 4).map((floor) => <span key={floor.key} title={floor.walls > 1 ? `Номер на одной из ${floor.walls} стен этой стороны` : undefined}><i className={`reason-marker ${floor.verdict}`} />{floor.floor_label || `${floor.floor} этаж`}{floor.building_id ? ` · ${floor.building_name || 'корпус по карте'}` : ''}</span>)}{floors.length > 4 && <small>и ещё {floors.length - 4}</small>}</div>}
        {category.reason && <p className="category-reason">{category.reason}</p>}
      </div>
    })}
  </div>
}

// Вопросы отеля — для паспорта ответа (текст вопроса, подписи полей); один запрос на отель.
const questionCache = new Map()
function useHotelQuestions(hotelId) {
  const [questions, setQuestions] = useState(null)
  useEffect(() => {
    if (!hotelId) return undefined
    let cancelled = false
    if (!questionCache.has(hotelId)) questionCache.set(hotelId, api(`/hotelier/${hotelId}/questions`).then((d) => d.questions || []).catch(() => { questionCache.delete(hotelId); return [] }))
    questionCache.get(hotelId).then((items) => { if (!cancelled) setQuestions(items) })
    return () => { cancelled = true }
  }, [hotelId])
  return questions
}

function FactRow({ fact, hotelId }) {
  const labels = useLabels()
  const [expanded, setExpanded] = useState(false)
  const hotelier = hotelierSource(fact)
  const questions = useHotelQuestions(expanded && hotelier ? hotelId : null)
  const question = hotelier && questions?.find((item) => String(item.question_id) === hotelier.questionId)
  const fieldLabels = Object.fromEntries((question?.fields || []).map((field) => [field.name, field.label]))
  const src = fact.sources?.[0]
  const params = paramWords(fact.params)
  const period = factPeriod(fact.params)
  const badge = [PROVENANCE[fact.status] || fact.status, fact.subject_level && fact.subject_level !== 'hotel' ? subjectLevelText(fact.subject_level) : ''].filter(Boolean).join(' · ')
  const date = formatDateTime(fact.calculated_at || fact.observed_at || src?.observed_at)
  return <div className="fact-row"><button className="fact-row-top" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><span className={`provenance-dot ${fact.status}`} /><span className="fact-label">{featureName(fact.feature_id, labels)}{period ? ` · ${period}` : ''}<small>{badge}</small></span><strong>{formatFactValue(fact, fieldLabels)}</strong><ChevronDown size={14} className={expanded ? 'rotated' : ''} /></button>{expanded && <div className="fact-source-detail">
    {hotelier && <>
      {hotelier.quote && <div><b>Ответ</b><span>«{hotelier.quote}»</span></div>}
      <div><b>Вопрос</b><span>{question?.question || (questions ? 'Вопрос анкеты отельеру' : 'Загружаем вопрос…')}</span></div>
      {hotelier.answeredAt && <div><b>Когда</b><span>{hotelier.answeredAt}</span></div>}
      <div><b>Кто ответил</b><span>{hotelier.respondent || (hotelier.simulated ? 'Имитатор отельера' : 'Отель')}{hotelier.simulated ? ' · ответ имитатора, только для показа' : ''}</span></div>
    </>}
    {!hotelier && <div><b>Источник</b><span>{src?.provider ? providerText(src.provider) : fact.status === 'unknown' ? 'Источник не найден' : 'Указан в паспорте'}{src?.license ? ` · ${src.license}` : ''}</span></div>}
    {fact.recipe_id && <div><b>Рецепт</b><span>{fact.recipe_id}</span></div>}
    <div><b>Область</b><span>{fact.scope_note || subjectLevelText(fact.subject_level)}</span></div>
    {!hotelier && <div><b>Дата</b><span>{date || 'Не указана'}{fact.freshness?.date_kind === 'pack_built_at' ? ' · дата сборки пакета' : ''}</span></div>}
    {fact.freshness?.refresh_due && <div><b>Обновить</b><span>{fact.freshness.stale ? 'пора обновить' : `до ${formatDay(fact.freshness.refresh_due.slice(0, 10))}`} · {fact.freshness.refresh_hint}</span></div>}
    {fact.confidence?.level && <div><b>Уверенность</b><span>{confidenceText(fact.confidence.level)}{fact.confidence.reason ? ` · ${fact.confidence.reason}` : ''}</span></div>}
    {(fact.unknown_reason_text || fact.unknown_reason) && <div><b>Почему неизвестно</b><span>{fact.unknown_reason_text || fact.unknown_reason}</span></div>}
    {fact.recipe_attempts?.length > 0 && <div><b>Попытки</b><span>{fact.recipe_attempts.slice(0, 3).map((attempt) => `${attempt.recipe_id}: ${attemptStateText(attempt.state)}${attempt.reason ? ` — ${attempt.reason}` : ''}`).join(' · ')}</span></div>}
    {params.length > 0 && <div><b>Параметры</b><span>{params.map(([key, value]) => `${key}: ${value}`).join(' · ')}</span></div>}
    {src?.url && <a href={src.url} target="_blank" rel="noreferrer">Открыть источник <ExternalLink size={13} /></a>}
  </div>}</div>
}

function WindowsRow({ row }) {
  const labels = useLabels()
  return <div className="fact-row windows-row"><div className="fact-row-top"><span className="provenance-dot computed" /><span className="fact-label">{featureName(row.featureId, labels)}<small>Факты окон</small></span><strong>{windowsText(row)}</strong></div><p className="windows-note">По каждому окну — свой факт; где именно «да» и «нет», сказано в объяснении выше.</p></div>
}

function BookingDialog({ result, ast, timeZone, stay, applyStay, booked, labels, onClose, onConfirm }) {
  const dialogRef = useRef(null)
  const headingRef = useRef(null)
  const [editingStay, setEditingStay] = useState(false)
  const [draft, setDraft] = useState(stay)
  useDialog(dialogRef, true, onClose)
  useEffect(() => { if (booked) headingRef.current?.focus() }, [booked])
  const hotel = result.hotel
  const today = cityToday(hotel.city_id, timeZone)
  const category = result.categories?.find((item) => item.room_type_id === result.best_category)
  const conditions = orderedItems(result)
  const mismatches = conditions.map((item) => periodMismatch(conditionParams(ast, item.atom_id), stay))
  const unresolved = (result.hard || []).filter((item) => verdictKind(item.verdict) !== 'yes')
  const error = stayError(stay, today)
  return <div className="modal-backdrop booking-backdrop"><section ref={dialogRef} tabIndex={-1} className="booking-dialog" role="dialog" aria-modal="true" aria-labelledby="booking-title">
    <button className="close-button" onClick={onClose} aria-label="Закрыть оформление"><X size={18} /></button>
    {booked && <div className="booking-check"><Check size={28} /></div>}
    <span className="section-kicker">{booked ? 'ВАШ ПЛАН ПОЕЗДКИ' : 'ОФОРМЛЕНИЕ ПОЕЗДКИ'}</span>
    <h2 id="booking-title" ref={headingRef} tabIndex={-1}>{booked ? labels.booked || 'Забронировано' : 'Проверьте условия'}</h2>
    <div className="imitation-note"><CircleHelp size={18} /><span>{labels.booking_imitation_note || 'Имитация оформления Островка. Реальная бронь не создаётся'}.</span></div>
    <p className="booking-hotel">{hotel.name}</p>
    <div className="booking-details">
      <span><CalendarDays size={18} /><span>{formatDateRange(stay.checkIn, stay.checkOut, true)}<small>{formatNights(nightCount(stay.checkIn, stay.checkOut))}</small></span></span>
      <span><Users size={18} />{formatGuests(stay.adults, stay.children)}</span>
      <span><BedDouble size={18} />{category?.name || 'Категория номера пока не выбрана'}</span>
    </div>
    {!booked && <button className="text-button booking-edit" aria-expanded={editingStay} onClick={() => { setDraft(stay); setEditingStay(!editingStay) }}>{editingStay ? 'Отменить изменение' : 'Изменить даты и гостей'}</button>}
    {editingStay && <div className="booking-edit-form"><StayFields value={draft} onChange={setDraft} today={today} /><p className="stay-api-note">Новые даты уйдут в запрос, и условия пересчитаются.</p><button className="primary-button full" disabled={Boolean(stayError(draft, today))} onClick={() => { setEditingStay(false); applyStay(draft) }}>Сохранить поездку</button></div>}
    {result.stale && <p className="stale-note">Подборка обновилась, а этого отеля нет на загруженной странице: условия ниже — по прежнему запросу.</p>}
    <p className="booking-availability">Наличие номеров и цена на эти даты ещё не проверены.</p>
    {conditions.length > 0 && <div className="booking-conditions"><h3>Ваши условия</h3>{conditions.map((item, index) => {
      const kind = verdictKind(item.verdict)
      const mismatch = mismatches[index]
      const fallback = kind === 'yes' ? (hasEstimate(item) ? 'Похоже, подходит — это оценка' : 'Выполнено по имеющимся данным') : kind === 'no' ? 'Условие не выполнено' : 'Нужно уточнить у отеля'
      return <div className={`booking-condition ${mismatch ? 'unknown' : kind}`} key={`${item.atom_id || item.feature_id}-${index}`}>
        <span className={`check-disc ${mismatch ? 'unknown' : kind}`}><VerdictIcon value={mismatch ? 'unknown' : item.verdict} size={14} /></span>
        <div><strong>{itemTitle(item, labels)}<small>{item.required ? 'Обязательно' : 'Желательно'}</small><Marks item={item} /></strong><p>{mismatch || item.reason || fallback}</p></div>
      </div>
    })}</div>}
    {mismatches.some(Boolean) && <div className="booking-warning"><CalendarDays size={18} /><span>Часть условий посчитана для других дат. Смените даты поездки — подборка пересчитается.</span></div>}
    {unresolved.length > 0 && <div className="booking-warning"><CircleHelp size={18} /><span>{unresolved.some((item) => verdictKind(item.verdict) === 'no') ? 'Отель не выполняет часть обязательных условий.' : 'Часть обязательных условий ещё нужно подтвердить у отеля.'} Это остаётся в силе и после имитации оформления.</span></div>}
    {!booked && error && <p className="stay-error" role="alert">{error}</p>}
    <button className="primary-button full" disabled={!booked && (editingStay || Boolean(error))} onClick={booked ? onClose : onConfirm}>{booked ? 'Вернуться к подборке' : 'Забронировать (имитация)'}{!booked && <ArrowRight size={16} />}</button>
  </section></div>
}

function HotelierView({ guestCity, cities, activeHotel, setActiveHotel, drafts, setDrafts, notify, searchData, refreshGuest, guestResults, showInSearch, tab, setTab }) {
  const labels = useLabels()
  const draftKey = (question) => `${question.hotel_id}:${question.question_id}`
  const placementKey = (question) => `placement:${question.hotel_id}:${question.room_type_id}`
  const [hotelCity, setHotelCity] = useState(guestCity)
  const [hotels, setHotels] = useState([])
  const [hotelFilter, setHotelFilter] = useState('')
  const [questions, setQuestions] = useState([])
  const [outcomes, setOutcomes] = useState({})
  const [questionFilter, setQuestionFilter] = useState('all')
  const [loading, setLoading] = useState(true)
  const [questionsLoading, setQuestionsLoading] = useState(false)
  const [pending, setPending] = useState({})
  const pendingRef = useRef({})
  const [placement, setPlacement] = useState({ state: 'loading', questions: [], choices: [], notes: [] })
  const [placementOutcomes, setPlacementOutcomes] = useState({})
  const [marks, setMarks] = useState({})
  const cityOptions = cities.length ? cities : Object.entries(cityNames).map(([city_id, name]) => ({ city_id, name }))

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api(`/hotels?city_id=${encodeURIComponent(hotelCity)}`)
      .then((d) => {
        if (cancelled) return
        const items = d.hotels || []
        setHotels(items)
        // Отель не сбрасывается на первый: остаётся выбранный или тот, что гость смотрел в подборке.
        setActiveHotel((current) => {
          if (items.some((hotel) => hotel.hotel_id === current)) return current
          const fromGuest = guestResults.find((result) => items.some((hotel) => hotel.hotel_id === result.hotel.hotel_id))
          return fromGuest?.hotel.hotel_id || items[0]?.hotel_id || ''
        })
      })
      .catch(() => { if (!cancelled) setHotels([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [hotelCity])

  useEffect(() => {
    if (!activeHotel) return undefined
    let cancelled = false
    setQuestions([])
    setQuestionsLoading(true)
    api(`/hotelier/${activeHotel}/questions`)
      .then((d) => { if (!cancelled) setQuestions(d.questions || []) })
      .catch(() => { if (!cancelled) setQuestions([]) })
      .finally(() => { if (!cancelled) setQuestionsLoading(false) })
    return () => { cancelled = true }
  }, [activeHotel])

  // Где окна номеров: вопрос на каждую категорию, корпуса отеля по карте (D17).
  useEffect(() => {
    if (!activeHotel) return undefined
    let cancelled = false
    setPlacement({ state: 'loading', questions: [], choices: [], notes: [] })
    api(`/hotelier/${activeHotel}/placement-questions`)
      .then((d) => { if (!cancelled) setPlacement({ state: 'ready', questions: d.questions || [], choices: d.building_choices || [], notes: d.notes || [] }) })
      .catch(() => { if (!cancelled) setPlacement({ state: 'error', questions: [], choices: [], notes: [] }) })
    return () => { cancelled = true }
  }, [activeHotel])

  function lock(question, action) {
    if (pendingRef.current[question.question_id]) return false
    pendingRef.current = { ...pendingRef.current, [question.question_id]: action }
    setPending(pendingRef.current)
    return true
  }
  function unlock(question) {
    const { [question.question_id]: _done, ...rest } = pendingRef.current
    pendingRef.current = rest
    setPending(rest)
  }

  async function askQuestion(question) {
    if (!lock(question, 'ask')) return
    try {
      const data = await api(`/hotelier/${question.hotel_id}/questions/${question.question_id}/ask`, { method: 'POST' })
      setQuestions((items) => items.map((item) => item.question_id === question.question_id ? { ...item, ...(data.question || {}) } : item))
      notify('Вопрос отмечен как отправленный отелю')
    } catch { notify('Не удалось отправить вопрос. Проверьте соединение с API.') }
    finally { unlock(question) }
  }

  // Песочница шлёт ответы как имитатор (D31): ответ со стенда не становится словами настоящего отеля.
  async function sendAnswer(question, payload, action) {
    try {
      const body = { question_id: question.question_id, is_simulated: true, respondent: 'Песочница', ...payload }
      if (searchData?.ast && searchData.ast.city_id === question.city_id) body.ast = searchData.ast
      const data = await api(`/hotelier/${question.hotel_id}/answers`, { method: 'POST', body: JSON.stringify(body) })
      setQuestions((items) => items.map((item) => item.question_id === question.question_id ? { ...item, ...(data.question || {}) } : item))
      setOutcomes((items) => ({ ...items, [question.question_id]: { ...data, action } }))
      if (payload.answer_text) setDrafts((items) => ({ ...items, [draftKey(question)]: payload.answer_text }))
      notify(data.state === 'answered' ? 'Ответ учтён — выдача уже пересчитана' : data.state === 'answered_unknown' ? 'Записали: отель не знает — это отдельный факт' : 'Агент не нашёл в тексте ответа на вопрос — выдача не изменилась')
      if (data.state !== 'answered_unparsed' && searchData?.ast?.city_id === question.city_id) refreshGuest()
    } catch (error) {
      notify(error.status === 422 ? 'Ответ не принят: поля не подходят к вопросу' : 'Не удалось сохранить ответ. Проверьте соединение с API.')
    }
  }
  async function answer(question, action) {
    let payload
    if (action === 'confirm') payload = { answer_text: confirmText(question.prefill) }
    else if (action === 'dont_know') payload = { dont_know: true, answer_text: null }
    else {
      const text = (drafts[draftKey(question)] ?? '').trim()
      if (!text) return notify('Напишите ответ своими словами')
      payload = { answer_text: text }
    }
    if (!lock(question, action)) return
    try { await sendAnswer(question, payload, action) } finally { unlock(question) }
  }
  async function simulate(question) {
    if (!lock(question, 'simulate')) return
    try {
      const data = await api(`/hotelier/${question.hotel_id}/simulate`, { method: 'POST', body: JSON.stringify({ question_id: question.question_id }) })
      if (!data.answer_text) return notify('Имитатор не нашёл, что ответить')
      setDrafts((items) => ({ ...items, [draftKey(question)]: data.answer_text }))
      await sendAnswer(question, { answer_text: data.answer_text, respondent: 'Имитатор отельера' }, 'simulate')
    } catch { notify('Имитатор не ответил. Проверьте соединение с API.') }
    finally { unlock(question) }
  }

  // Имитатор только заполняет ответ и отмечает корпуса — отправляет отельер (видно, что уйдёт).
  async function simulatePlacement(question) {
    if (!lock(question, 'simulate')) return
    try {
      const data = await api(`/hotelier/${question.hotel_id}/placement-simulate`, { method: 'POST', body: JSON.stringify({ room_type_id: question.room_type_id }) })
      if (!data.answer_text) return notify('Имитатор не нашёл, что ответить')
      setDrafts((items) => ({ ...items, [placementKey(question)]: data.answer_text }))
      setMarks((items) => ({ ...items, [question.room_type_id]: marksFromAliases(data.building_aliases) }))
      notify(data.notes?.[0] || 'Имитатор ответил — проверьте текст и корпус и отправьте')
    } catch { notify('Имитатор не ответил. Проверьте соединение с API.') }
    finally { unlock(question) }
  }
  function setMark(question, buildingId, change) {
    setMarks((items) => {
      const current = items[question.room_type_id] || {}
      const mark = current[buildingId] || { checked: false, name: '' }
      const known = placement.choices.find((item) => item.building_id === buildingId)?.hotel_aliases?.[0] || ''
      const next = { ...mark, ...change }
      if (change.checked && !mark.name && known) next.name = known
      return { ...items, [question.room_type_id]: { ...current, [buildingId]: next } }
    })
  }
  // Песочница: is_simulated (D41-8). Ответ учтён — пересчитываем подборку гостя того же города
  // (уже после разблокировки карточки: поиск по всему городу идёт несколько секунд).
  async function sendPlacement(question) {
    const { body, error } = placementBody(question, drafts[placementKey(question)] ?? '', marks[question.room_type_id])
    if (error) return notify(error)
    if (!lock(question, 'send')) return
    const rt = question.room_type_id
    const sameCity = searchData?.ast?.city_id === hotelCity
    const before = sameCity ? searchCheck(guestResults, question.hotel_id, rt) : null
    let recheck = false
    try {
      const data = await api(`/hotelier/${question.hotel_id}/placement-answers`, { method: 'POST', body: JSON.stringify(body) })
      recheck = data.state === 'answered' && sameCity
      setPlacement((current) => ({ ...current, questions: current.questions.map((item) => item.room_type_id === rt ? questionAfter(item, data) : item) }))
      setPlacementOutcomes((items) => ({ ...items, [rt]: { summary: placementOutcome(data, question), before, after: null, checking: recheck, checked: false, query: sameCity ? searchData.ast.raw_text : '' } }))
      notify(data.state === 'answered' ? 'Размещения сохранены — пересчитываем выдачу' : data.state === 'needs_clarification' ? 'Нужно уточнение — размещения не сохранены' : data.state === 'real_answer_kept' ? 'Настоящий ответ отеля остаётся — ответ имитатора не сохранён' : 'Размещения не к чему привязать')
    } catch (error) {
      notify(error.status === 422 ? 'Ответ не принят: проверьте ответ и названия корпусов' : 'Не удалось сохранить ответ. Проверьте соединение с API.')
    } finally { unlock(question) }
    if (!recheck) return
    const fresh = await refreshGuest()
    setPlacementOutcomes((items) => items[rt]?.checking ? { ...items, [rt]: { ...items[rt], after: fresh ? searchCheck(fresh.results, question.hotel_id, rt) : null, checking: false, checked: Boolean(fresh) } } : items)
  }

  const matches = useMemo(() => {
    const needle = hotelFilter.trim().toLowerCase()
    return (needle ? hotels.filter((hotel) => (hotel.name || '').toLowerCase().includes(needle)) : hotels).slice(0, 100)
  }, [hotels, hotelFilter])
  const active = hotels.find((hotel) => hotel.hotel_id === activeHotel)
  const visibleQuestions = questions.filter((question) => {
    const answered = ['answered', 'answered_unknown', 'answered_unparsed'].includes(question.answer_status)
    return questionFilter === 'all' || (questionFilter === 'answered' ? answered : !answered)
  })
  const questionFilterLabel = { all: 'Все вопросы', unanswered: 'Без ответа', answered: 'С ответом' }[questionFilter]
  const visiblePlacement = placement.questions.filter((question) => questionFilter === 'all' || (questionFilter === 'answered') === placementState(question).answered)
  const guestSearchHere = searchData?.ast?.city_id === hotelCity
  return <div className="workspace-view hotelier-view"><div className="content-topline"><div className="breadcrumb">Командный кабинет <span>/</span> Для отельера</div><span className="data-depth"><i />Песочница</span></div><div className="workspace-heading"><div className="workspace-icon hotelier"><MessageCircle size={22} /></div><div><span className="section-kicker">КАБИНЕТ ПАРТНЁРА</span><h1>Помогите гостям<br />узнать <em>больше.</em></h1><p>Ответьте на вопросы о вашем отеле. Мы покажем гостям, что подтверждено, а что ещё нужно уточнить.</p></div></div>
    <div className="hotel-select-card"><div className="hotel-picker"><span className="section-kicker">ВАШ ОБЪЕКТ</span><div className="hotel-picker-row"><select aria-label="Город" value={hotelCity} onChange={(e) => { setHotelCity(e.target.value); setHotelFilter('') }}>{cityOptions.map((item) => <option key={item.city_id} value={item.city_id}>{item.name}</option>)}</select><input type="search" aria-label="Найти отель по названию" placeholder={`Найти среди ${hotels.length} отелей`} value={hotelFilter} onChange={(e) => setHotelFilter(e.target.value)} /></div><select aria-label="Отель" value={activeHotel} onChange={(e) => setActiveHotel(e.target.value)}>{active && !matches.includes(active) && <option value={active.hotel_id}>{active.name}</option>}{matches.map((hotel) => <option key={hotel.hotel_id} value={hotel.hotel_id}>{hotel.name}</option>)}</select>{hotels.length > matches.length && !hotelFilter && <small>Показаны первые {matches.length} — уточните название в поиске</small>}</div><span className="sim-tag">ПЕСОЧНИЦА</span></div>
    <div className="hotelier-banner"><Sparkles size={18} /><div><strong>Ответ сразу разбирает агент-отельер</strong><span>Без ключа LLM — правилами, с ключом — моделью. Поля с цитатами становятся фактом и сразу пересчитывают выдачу; что понял агент, видно под ответом. В песочнице ответы сохраняются с пометкой «ответ имитатора» — для показа, а не как слова настоящего отеля.</span></div></div>
    <div className="workspace-section-head"><div><span className="section-kicker">ВОПРОСЫ ПО ОТЕЛЮ</span><h2>{tab === 'placement' ? 'Где окна номеров' : 'Вопросы по отелю'} <span className="count-bubble">{tab === 'placement' ? visiblePlacement.length : visibleQuestions.length}</span></h2></div><button className="filter-button" onClick={() => setQuestionFilter((value) => value === 'all' ? 'unanswered' : value === 'unanswered' ? 'answered' : 'all')}><ArrowDownUp size={14} /> {questionFilterLabel}</button></div>
    <div className="status-filter question-tabs" role="group" aria-label="Какие вопросы показать">{[['questions', 'Об отеле', questions.length], ['placement', 'Где окна номеров', placement.questions.length]].map(([value, label, count]) => <button key={value} className={`filter-pill ${tab === value ? 'active' : ''}`} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}<b>{count}</b></button>)}</div>
    {loading && <div className="loading-inline">Загружаем отели…</div>}{!loading && !hotels.length && <div className="empty-state"><CircleHelp size={28} /><h3>Нет данных отелей</h3><p>Подключите API, чтобы загрузить вопросы.</p></div>}
    {tab === 'placement' && <>
      <p className="placement-intro">По сторонам и этажам окон считаем вид и закат для каждого номера категории. Сторона света — все стены корпуса на эту сторону по карте, номер — у одной из них. Напишите, если других вариантов нет: иначе гость увидит «Нужно уточнить».</p>
      {placement.state === 'loading' && activeHotel && <div className="loading-inline">Загружаем категории номеров…</div>}
      {placement.state === 'error' && <div className="loading-inline">Не удалось загрузить категории. Проверьте соединение с API.</div>}
      {placement.notes.map((note) => <div className="loading-inline" key={note}>{note}</div>)}
      <div className="question-list">{visiblePlacement.map((question) => {
        const state = placementState(question)
        const outcome = placementOutcomes[question.room_type_id]
        const busy = pending[question.question_id]
        const choices = question.building_choices || []
        const roomMarks = marks[question.room_type_id] || {}
        const inputId = `placement-${question.question_id}`
        const sidesOf = (buildingId) => wallSides(placement.choices.find((item) => item.building_id === buildingId))
        return <article className="question-card placement-card" key={question.question_id}>
          <div className="question-card-top"><span className="question-feature">{question.title}</span><span className={`question-state ${state.answered ? 'answered' : ''}`}><span />{state.text}</span></div>
          <h3>{placementQuestionText(question)}</h3>
          {choices.length > 1 && <fieldset className="building-choices"><legend>КОРПУС — ОТМЕТЬТЕ ТОТ, О КОТОРОМ ОТВЕТ</legend>{choices.map((choice) => {
            const mark = roomMarks[choice.building_id] || { checked: false, name: '' }
            return <div className={`building-choice ${mark.checked ? 'checked' : ''}`} key={choice.building_id}><div className="building-choice-row"><label title={choice.label}><input type="checkbox" checked={mark.checked} onChange={(e) => setMark(question, choice.building_id, { checked: e.target.checked })} /><span>{buildingLabel(choice.label)}{sidesOf(choice.building_id) && <small>Стены по карте: {sidesOf(choice.building_id)}</small>}</span></label>{osmUrl(choice.building_id) && <a className="building-map-link" href={osmUrl(choice.building_id)} target="_blank" rel="noreferrer" aria-label={`Корпус на карте: ${buildingLabel(choice.label)}`}>на карте <ExternalLink size={11} /></a>}</div>{mark.checked && <input type="text" aria-label="Как вы называете этот корпус" placeholder="Как вы его называете: «корпус 2», «главный»…" value={mark.name} onChange={(e) => setMark(question, choice.building_id, { name: e.target.value })} />}</div>
          })}</fieldset>}
          {choices.length === 1 && <p className="building-single">Корпус по карте: {buildingLabel(choices[0].label)}{sidesOf(choices[0].building_id) && <small>Стены по карте: {sidesOf(choices[0].building_id)}</small>}</p>}
          <label htmlFor={inputId}>ГДЕ ОКНА НОМЕРОВ</label>
          <textarea id={inputId} rows="3" value={drafts[placementKey(question)] ?? ''} onChange={(e) => setDrafts((items) => ({ ...items, [placementKey(question)]: e.target.value }))} placeholder="Например: окна на юго-запад, с 9 по 15 этаж. Других вариантов нет." />
          <div className="question-evidence"><span><ShieldCheck size={14} />В песочнице ответ сохранится с пометкой «ответ имитатора» и не заменит ответ настоящего отеля</span></div>
          <div className="decision-row"><span>ПОДСКАЗКА</span><div><button className="decision-button simulated" disabled={Boolean(busy)} onClick={() => simulatePlacement(question)}><Sparkles size={12} />{busy === 'simulate' ? 'Имитатор отвечает…' : 'Ответ имитатора'}</button></div></div>
          {outcome && <PlacementOutcome outcome={outcome} guestSearchHere={guestSearchHere} onCheck={() => showInSearch(question.hotel_id, hotelCity)} />}
          <div className="question-actions"><div className="question-api-actions"><span className="local-note">Понятое сохранится, о непонятом агент задаст уточняющий вопрос.</span></div><button className="primary-button" disabled={Boolean(busy)} onClick={() => sendPlacement(question)}>{busy === 'send' ? 'Сохраняем…' : 'Отправить'} <ArrowRight size={15} /></button></div>
        </article>
      })}</div>
      {placement.state === 'ready' && !placement.notes.length && !visiblePlacement.length && <div className="loading-inline">{placement.questions.length ? 'Нет вопросов с таким фильтром.' : 'У отеля нет категорий номеров.'}</div>}
    </>}
    {tab === 'questions' && questionsLoading && <div className="loading-inline">Загружаем вопросы отеля…</div>}
    {tab === 'questions' && <div className="question-list">{visibleQuestions.map((question) => {
      const answered = ['answered', 'answered_unknown', 'answered_unparsed'].includes(question.answer_status)
      const outcome = outcomes[question.question_id]
      const busy = pending[question.question_id]
      const inputId = `answer-${question.question_id}`
      return <article className="question-card" key={question.question_id || question.feature_id}>
        <div className="question-card-top"><span className="question-feature">{question.title || featureName(question.feature_id, labels)}</span><span className={`question-state ${answered ? 'answered' : question.answer_status === 'asked' ? 'asked' : ''}`}><span />{ANSWER_STATES[question.answer_status] || 'Нужно уточнить'}</span></div>
        <h3>{question.question}</h3>
        {question.prefill && <div className="prefill-summary"><span>НА СТРАНИЦЕ ОСТРОВКА</span><p>{question.prefill}</p></div>}
        <label htmlFor={inputId}>ДОПОЛНЕНИЕ ИЛИ ИСПРАВЛЕНИЕ</label>
        <textarea id={inputId} rows="3" value={drafts[draftKey(question)] ?? question.answer_text ?? ''} onChange={(e) => setDrafts((items) => ({ ...items, [draftKey(question)]: e.target.value }))} placeholder="Напишите ответ своими словами…" />
        <div className="question-evidence"><span><ShieldCheck size={14} />Ответ привяжется к вопросу; в песочнице гость увидит пометку «ответ имитатора»</span></div>
        <div className="decision-row"><span>БЫСТРЫЙ ОТВЕТ</span><div>
          <button className="decision-button" disabled={Boolean(busy) || !question.prefill} title={question.prefill ? 'Подтвердить сведения со страницы Островка' : 'На странице Островка об этом ничего нет'} onClick={() => answer(question, 'confirm')}>{busy === 'confirm' ? 'Отправляем…' : 'Верно'}</button>
          <button className="decision-button" disabled={Boolean(busy)} onClick={() => answer(question, 'dont_know')}>{busy === 'dont_know' ? 'Отправляем…' : 'Не знаю'}</button>
          <button className="decision-button simulated" disabled={Boolean(busy)} onClick={() => simulate(question)}><Sparkles size={12} />{busy === 'simulate' ? 'Имитатор отвечает…' : 'Ответ имитатора'}</button>
        </div></div>
        {outcome && <AnswerOutcome outcome={outcome} question={question} />}
        <div className="question-actions"><div className="question-api-actions">{question.answer_status === 'not_asked' && <button className="ask-question-button" disabled={Boolean(busy)} onClick={() => askQuestion(question)}>{busy === 'ask' ? 'Отправляем…' : 'Отправить вопрос отелю'}</button>}{question.answer_status === 'asked' && <span className="asked-note"><Check size={13} /> Вопрос отправлен</span>}<span className="local-note">Если в тексте не найдётся ответа на вопрос, он сохранится без факта.</span></div><button className="primary-button" disabled={Boolean(busy)} onClick={() => answer(question, 'text')}>{busy === 'text' ? 'Сохраняем…' : 'Отправить ответ'} <ArrowRight size={15} /></button></div>
      </article>
    })}</div>}
  </div>
}

// Итог ответа о размещении: что сохранено, что понял агент, уточнение и как это видит гость.
function PlacementOutcome({ outcome, guestSearchHere, onCheck }) {
  const { summary, before, after, checking, checked, query } = outcome
  return <div className={`answer-outcome ${summary.tone === 'pending' ? 'pending' : ''}`}>
    <strong>{summary.title} · песочница, с пометкой «ответ имитатора»</strong>
    {summary.lines.map((line) => <span key={line}>{line}</span>)}
    {summary.variants.length > 0 && <div className="agent-fields"><small>Агент понял{summary.parsedBy === 'llm' ? ' (модель)' : ' (правила)'}:</small><ul>{summary.variants.map((variant, index) => <li key={index}><b>{variant.building}:</b> {variant.floors}, {variant.sides}{variant.quote && <q>{variant.quote}</q>}</li>)}</ul></div>}
    {summary.rejected.length > 0 && <small>Не принято: {summary.rejected.join('; ')}</small>}
    {summary.followup && <span className="agent-followup placement-followup">{summary.state === 'answered' ? 'Уточняющий вопрос' : 'Что уточнить'}: {summary.followup}</span>}
    {summary.notes.map((note) => <small key={note}>{note}</small>)}
    {summary.state === 'answered' && <div className="placement-check">
      {checking && <span>Пересчитываем подборку гостя…</span>}
      {after && <span>В подборке гостя{query ? ` по запросу «${query}»` : ''}: {before && before.statusLabel !== after.statusLabel ? `${before.statusLabel} → ` : ''}<b>{after.statusLabel}</b>{after.category ? ` · «${after.category.name}» — ${after.category.statusLabel}` : ''}</span>}
      {after?.category?.reason && <small>{after.category.reason}</small>}
      {checked && !after && <span>Отеля нет на первой странице подборки гостя.</span>}
      {!guestSearchHere && <span>Подборка гостя сейчас не по городу отеля — по кнопке найдём по тому же запросу в городе отеля.</span>}
      <button className="inline-action" disabled={checking} onClick={onCheck}><Search size={13} />Проверить в поиске</button>
    </div>}
  </div>
}

function AnswerOutcome({ outcome, question }) {
  const fields = agentFields(outcome.agent, question.fields)
  const title = outcome.state === 'answered_unknown' ? 'Записали: отель не знает' : outcome.state === 'answered_unparsed' ? 'Агент не нашёл в тексте ответа на вопрос' : 'Ответ учтён'
  const via = outcome.action === 'simulate' ? 'ответ имитатора' : 'песочница, с пометкой «ответ имитатора»'
  const notes = (outcome.notes || []).filter((note) => !/имитатор|офлайн|ключ LLM/i.test(note))
  return <div className={`answer-outcome ${outcome.state === 'answered_unparsed' ? 'pending' : ''}`}>
    <strong>{title} · {via}</strong>
    {outcome.before && outcome.after && <span>Статус отеля по текущему запросу: {outcome.before.status_label || outcome.before.status} → {outcome.after.status_label || outcome.after.status}</span>}
    {fields.length > 0 && <div className="agent-fields"><small>Агент понял{outcome.agent?.parsed_by === 'llm' ? ' (модель)' : ' (правила)'}:</small><ul>{fields.map((field) => <li key={field.name}><b>{field.label}:</b> {field.value}{field.quote && <q>{field.quote}</q>}</li>)}</ul></div>}
    {outcome.agent?.rejected?.length > 0 && <small>Не принято: {outcome.agent.rejected.map((item) => typeof item === 'string' ? item : `${item.field || ''}${item.reason ? ` — ${item.reason}` : ''}`).join('; ')}</small>}
    {outcome.agent?.followup && <span className="agent-followup">Уточняющий вопрос: {outcome.agent.followup}</span>}
    {(outcome.conflict_questions || []).map((item) => <span className="agent-conflict" key={item.text}>Расходится с нашим расчётом: {item.text}</span>)}
    {notes.map((note) => <small key={note}>{note}</small>)}
  </div>
}

// Очередь ручной проверки (D33): что машина не решила сама — к какому зданию относится отель.
// Человек выбирает здание по доказательствам (адрес на карте, точка отеля в контуре) или говорит
// «ни одно» / «не уверен»; решение записывается с именем и временем.
const CURATION_STATES = { proposed: 'ждут проверки', accepted: 'приняты', rejected: 'ни одно здание', unsure: 'не уверены' }
const MATCH_WORDS = { exact: 'совпадает', similar: 'похож', partial: 'частично', different: 'не совпадает' }

function CurationQueue({ city, notify }) {
  const [data, setData] = useState({ state: 'loading', items: [], counts: {}, total: 0 })
  const [reviewer, setReviewer] = useState(() => { try { return localStorage.getItem('curation-reviewer') || '' } catch { return '' } })
  const [busy, setBusy] = useState('')
  const load = useCallback(() => {
    setData((current) => ({ ...current, state: 'loading' }))
    api(`/curation?city_id=${encodeURIComponent(city)}&state=proposed&limit=5`)
      .then((d) => setData({ state: 'ready', items: d.items || [], counts: d.counts || {}, total: d.total || 0 }))
      .catch(() => setData({ state: 'error', items: [], counts: {}, total: 0 }))
  }, [city])
  useEffect(() => { load() }, [load])
  async function review(item, state, buildingId = null) {
    const name = reviewer.trim()
    if (!name) { notify('Укажите, кто проверяет — решение записывается с именем'); return }
    try { localStorage.setItem('curation-reviewer', name) } catch { /* без памяти браузера — не страшно */ }
    setBusy(item.id)
    try {
      await api(`/curation/${item.id}/review`, { method: 'POST', body: JSON.stringify({ state, reviewer: name, building_id: buildingId }) })
      const dup = item.kind === 'duplicate_listing'
      notify(state === 'accepted' ? (dup ? 'Записали: это один объект' : 'Записали: здание отеля подтверждено') : state === 'rejected' ? (dup ? 'Записали: разные объекты' : 'Записали: ни одно здание не подходит') : 'Записали: не уверены — вернётся позже')
      load()
    } catch { notify('Не получилось записать решение — попробуйте ещё раз') } finally { setBusy('') }
  }
  const counts = Object.entries(CURATION_STATES).filter(([key]) => data.counts[key]).map(([key, label]) => `${label}: ${data.counts[key]}`).join(' · ')
  return <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">D33 · РУЧНАЯ ПРОВЕРКА</span><h2>На проверку <span className="count-bubble">{data.total}</span></h2></div><button className="inline-action" onClick={load}>Обновить</button></div>
    <p className="panel-note">Машина не уверена, к какому зданию относится отель, или видит одну карточку дважды. Решите по доказательствам — от здания зависят этажность, вид и закат из окон. {counts}</p>
    <label className="reviewer-field">Кто проверяет <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="Имя" maxLength={80} /></label>
    {data.state === 'loading' && <div className="loading-inline">Загружаем очередь…</div>}
    {data.state === 'error' && <div className="empty-facts">Очередь сейчас недоступна.</div>}
    {data.state === 'ready' && !data.items.length && <div className="empty-facts">Очередь пуста — всё проверено.</div>}
    {data.items.map((item) => item.kind === 'duplicate_listing' ? <div key={item.id} className="curation-item">
      <strong>Похоже на дубль объявления: {item.listings[0]?.name || item.hotel_name}</strong>
      <ul className="curation-candidates">{item.listings.map((l) => <li key={l.hotel_id}><span><a href={l.url} target="_blank" rel="noreferrer">{l.hotel_id}</a> — {l.address}{l.room_types != null && ` · категорий: ${l.room_types}`}</span></li>)}</ul>
      <div className="curation-actions"><button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'accepted')}>Это один объект</button><button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'rejected')}>Разные объекты</button><button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'unsure')}>Не уверен</button><small>≈ {item.estimated_review_minutes || 2} мин · предложил {item.proposed_by}</small></div>
    </div> : <div key={item.id} className="curation-item">
      <strong>{item.hotel_name || `Отель ${item.subject_id}`}</strong>
      <small>Адрес отеля: {item.hotel_address || '—'}{item.latitude != null && <> · <a href={`https://www.openstreetmap.org/?mlat=${item.latitude}&mlon=${item.longitude}#map=19/${item.latitude}/${item.longitude}`} target="_blank" rel="noreferrer">точка на карте <ExternalLink size={12} /></a></>}</small>
      {item.candidates.length ? <ul className="curation-candidates">{item.candidates.map((c) => <li key={c.building_id}>
        <span><a href={c.url} target="_blank" rel="noreferrer">{c.building_id}</a> — {c.osm_address || 'адреса на карте нет'}{c.house && ` · дом ${MATCH_WORDS[c.house] || c.house}`}{c.street && `, улица ${MATCH_WORDS[c.street] || c.street}`}{c.point_in_building != null && ` · точка отеля ${c.point_in_building ? 'в контуре' : 'вне контура'}`}</span>
        <button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'accepted', c.building_id)}>Это здание</button>
      </li>)}</ul> : <p className="condition-reason">Кандидатов с полным адресом нет — проверьте точку на карте.</p>}
      <div className="curation-actions"><button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'rejected')}>Ни одно не подходит</button><button className="quiet-button" disabled={busy === item.id} onClick={() => review(item, 'unsure')}>Не уверен</button><small>≈ {item.estimated_review_minutes || 3} мин · предложил {item.proposed_by}</small></div>
    </div>)}
  </section>
}

function AdminView({ guestCity, cities, notify }) {
  const labels = useLabels()
  const [panelCity, setPanelCity] = useState(guestCity)
  const [coverage, setCoverage] = useState([])
  const [conflicts, setConflicts] = useState([])
  const [factConflicts, setFactConflicts] = useState([])
  const [journal, setJournal] = useState({ state: 'loading', items: [], days: 30 })
  const [showAllCoverage, setShowAllCoverage] = useState(false)
  const [showAllConflicts, setShowAllConflicts] = useState(false)
  const [showAllFactConflicts, setShowAllFactConflicts] = useState(false)
  const [featureText, setFeatureText] = useState('')
  const [specDraft, setSpecDraft] = useState(null)
  const [yamlText, setYamlText] = useState('')
  const [trial, setTrial] = useState(null)
  const [specBusy, setSpecBusy] = useState('')
  const [activeFeatures, setActiveFeatures] = useState([])
  const [activation, setActivation] = useState(null)
  const [refreshVersion, setRefreshVersion] = useState(0)
  // Актуализация: что и когда пора обновить — на сегодня или на выбранную дату.
  const [asOf, setAsOf] = useState(() => new Date().toISOString().slice(0, 10))
  const [fresh, setFresh] = useState({ state: 'loading', rows: [] })
  const [showAllFresh, setShowAllFresh] = useState(false)
  useEffect(() => {
    let cancelled = false
    setFresh((current) => ({ ...current, state: 'loading' }))
    api(`/cities/${panelCity}/freshness?as_of=${asOf}T00:00:00Z`).then((d) => { if (!cancelled) setFresh({ state: 'ready', rows: d.rows || [] }) }).catch(() => { if (!cancelled) setFresh({ state: 'error', rows: [] }) })
    return () => { cancelled = true }
  }, [panelCity, asOf, refreshVersion])
  const cityOptions = cities.length ? cities : Object.entries(cityNames).map(([city_id, name]) => ({ city_id, name }))
  useEffect(() => {
    let cancelled = false
    api(`/cities/${panelCity}/coverage`).then((d) => { if (!cancelled) setCoverage(d.features || []) }).catch(() => { if (!cancelled) setCoverage([]) })
    api(`/conflicts?city_id=${panelCity}`).then((d) => { if (!cancelled) { setConflicts(d.conflicts || []); setFactConflicts(d.fact_conflicts || []) } }).catch(() => { if (!cancelled) { setConflicts([]); setFactConflicts([]) } })
    setJournal((current) => ({ ...current, state: 'loading' }))
    api('/specs').then((d) => { if (!cancelled) setActiveFeatures(d.features || []) }).catch(() => { if (!cancelled) setActiveFeatures([]) })
    api(`/out-of-catalog?city_id=${panelCity}`).then((d) => { if (!cancelled) setJournal({ state: 'ready', items: d.items || [], days: d.window_days || 30 }) }).catch(() => { if (!cancelled) setJournal({ state: 'error', items: [], days: 30 }) })
    return () => { cancelled = true }
  }, [panelCity, refreshVersion])
  const cityInfo = cities.find((item) => item.city_id === panelCity)
  const hotelCount = cityInfo?.hotels ?? cityInfo?.hotels_in_sample ?? null
  const counted = coverage.filter((row) => Number(row.computed || 0) + Number(row.estimate || 0) > 0).length
  const coverRows = showAllCoverage ? coverage : coverage.slice(0, 8)
  const conflictRows = showAllConflicts ? conflicts : conflicts.slice(0, 4)
  const factConflictRows = showAllFactConflicts ? factConflicts : factConflicts.slice(0, 4)

  async function prepareFeatureDraft(event, text = featureText) {
    event?.preventDefault()
    if (text.trim().length < 3) return notify('Опишите характеристику хотя бы парой слов')
    setFeatureText(text)
    setSpecBusy('draft')
    setTrial(null)
    setActivation(null)
    try {
      const data = await api('/specs/draft', { method: 'POST', body: JSON.stringify({ text: text.trim() }) })
      setSpecDraft(data)
      setYamlText(data.yaml || '')
    } catch (error) { notify(error.status === 422 ? 'Текст слишком короткий или длинный для черновика' : 'Не удалось подготовить черновик. Проверьте соединение с API.') }
    finally { setSpecBusy('') }
  }
  async function activateFeature() {
    setSpecBusy('activate')
    try {
      const result = await api('/specs/activate', { method: 'POST', body: JSON.stringify({ yaml: yamlText }) })
      setActivation(result)
      setRefreshVersion((v) => v + 1)
      notify(`Характеристика включена: посчитана для ${result.facts} отелей, поиск её уже знает`)
    } catch (error) { notify(error.status === 422 ? 'Не включить: в спецификации есть проблемы' : 'Включение не удалось. Проверьте соединение с API.') }
    finally { setSpecBusy('') }
  }
  async function runTrial() {
    setSpecBusy('trial')
    try {
      setTrial(await api('/specs/trial', { method: 'POST', body: JSON.stringify({ yaml: yamlText, city_id: panelCity }) }))
    } catch (error) { notify(error.status === 422 ? 'YAML не читается как спецификация — поправьте черновик' : 'Пробный расчёт не удался. Проверьте соединение с API.') }
    finally { setSpecBusy('') }
  }

  return <div className="workspace-view admin-view"><div className="content-topline"><div className="breadcrumb">Командный кабинет <span>/</span> Панель команды</div><span className="data-depth"><i />Данные API</span></div><div className="workspace-heading"><div className="workspace-icon admin"><SlidersHorizontal size={22} /></div><div><span className="section-kicker">УПРАВЛЕНИЕ ДАННЫМИ</span><h1>Видим пробелы.<br /><em>Закрываем их.</em></h1><p>Сверяйте качество покрытия, разногласия между источниками и то, что гости спрашивают, а мы пока не знаем.</p></div></div>
    <div className="admin-filters"><label>ГОРОД ПАНЕЛИ<select value={panelCity} onChange={(e) => setPanelCity(e.target.value)}>{cityOptions.map((item) => <option key={item.city_id} value={item.city_id}>{item.name}</option>)}</select></label><button className="filter-button" onClick={() => { setRefreshVersion((value) => value + 1); notify('Запросили свежие данные из API') }}><RotateCcw size={14} /> Обновить</button></div>
    <div className="admin-metrics"><Metric label="ОТЕЛЕЙ В ГОРОДЕ" value={hotelCount ?? '—'} hint="По каталогу города" /><Metric label="ХАРАКТЕРИСТИК В КАТАЛОГЕ" value={coverage.length || '—'} hint={coverage.length ? `Посчитано хотя бы у одного отеля: ${counted}` : 'Покрытие не загружено'} /><Metric label="РАЗНОГЛАСИЙ" value={conflicts.length + factConflicts.length || '—'} hint="Источники и ответы отеля" /></div>
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">ПОКРЫТИЕ ХАРАКТЕРИСТИК</span><h2>Что мы знаем о городе</h2></div><div className="panel-actions"><span className="coverage-legend"><i />посчитано <i className="estimate" />оценка</span>{coverage.length > 8 && <button className="inline-action" onClick={() => setShowAllCoverage(!showAllCoverage)}>{showAllCoverage ? 'Свернуть' : `Все ${coverage.length}`}</button>}</div></div>{coverRows.length ? <div className="coverage-table">{coverRows.map((row, i) => {
      const total = Number(row.N || 0)
      const notApplicable = Number(row.not_applicable || 0)
      const base = Math.max(total - notApplicable, 0)
      const computed = Number(row.computed || 0)
      const estimate = Number(row.estimate || 0)
      return <div className="coverage-row" key={row.feature_id || i}><span className="coverage-name"><b>{row.feature_id}</b><span>{labels.features?.[row.feature_id] || row.feature_name || 'Характеристика'}{notApplicable > 0 && <small>неприменимо у {notApplicable}</small>}</span></span><div className="coverage-bar" title={`${computed} посчитано, ${estimate} оценок, ${row.unknown || 0} неизвестно${notApplicable ? `, ${notApplicable} неприменимо` : ''}`}><span className="coverage-computed" style={{ width: `${base ? computed / base * 100 : 0}%` }} /><span className="coverage-estimate" style={{ width: `${base ? estimate / base * 100 : 0}%` }} /></div><span className="coverage-value">{computed + estimate}/{base || '—'}</span></div>
    })}</div> : <div className="empty-facts">Сводка покрытия появится, когда API вернёт заполненные показатели.</div>}</section>
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">АКТУАЛИЗАЦИЯ</span><h2>Свежесть данных {fresh.rows.some((r) => r.stale) && <span className="count-bubble">{fresh.rows.reduce((n, r) => n + Number(r.stale || 0), 0)}</span>}</h2></div><div className="panel-actions"><label className="as-of">на дату <input type="date" value={asOf} onChange={(e) => e.target.value && setAsOf(e.target.value)} /></label>{fresh.rows.length > 8 && <button className="inline-action" onClick={() => setShowAllFresh(!showAllFresh)}>{showAllFresh ? 'Свернуть' : `Все ${fresh.rows.length}`}</button>}</div></div>
      <p className="panel-note">У каждого значения — дата и срок обновления: свой у факта (климат — через год) или по виду данных (страница Островка — 30 дней, расчёт по картам — 180). Устаревшее не выбрасывается: ответ по нему помечается «пора обновить».</p>
      {fresh.state === 'error' && <div className="empty-facts">Не удалось загрузить сводку свежести.</div>}
      {fresh.rows.length > 0 && <div className="fresh-table">{(showAllFresh ? fresh.rows : fresh.rows.slice(0, 8)).map((r, i) => <div className={`fresh-row ${r.stale ? 'stale' : ''}`} key={`${r.feature_id}-${r.status}-${r.recipe}-${i}`}><span className="coverage-name"><b>{r.feature_id}</b><span>{labels.features?.[r.feature_id] || r.feature_id}<small>{PROVENANCE[r.status] || r.status} · {r.recipe || 'рецепт не указан'}</small></span></span><span className="fresh-dates"><small>значений {formatNumber(r.n)} · с {r.oldest ? formatDay(r.oldest.slice(0, 10)) : '—'}</small><b>{r.stale ? `устарело: ${formatNumber(r.stale)}` : `обновить до ${r.next_due ? formatDay(r.next_due.slice(0, 10)) : '—'}`}</b></span><span className="fresh-hint">{r.refresh_hint}</span></div>)}</div>}
    </section>
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">РАЗНОГЛАСИЯ ИСТОЧНИКОВ</span><h2>Нужно разобраться <span className="count-bubble">{conflicts.length}</span></h2></div>{conflicts.length > 4 && <button className="inline-action" onClick={() => setShowAllConflicts(!showAllConflicts)}>{showAllConflicts ? 'Свернуть' : 'Все разногласия'}</button>}</div>{conflictRows.map((item, i) => <ConflictRow key={item.hotel_id + item.feature_id + i} item={item} />)}{!conflicts.length && <div className="empty-facts">В выбранном городе разногласий пока нет или API ещё не загрузил их.</div>}</section>
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">ОТВЕТЫ ОТЕЛЯ ПРОТИВ РАСЧЁТОВ</span><h2>Противоречия фактов <span className="count-bubble">{factConflicts.length}</span></h2></div>{factConflicts.length > 4 && <button className="inline-action" onClick={() => setShowAllFactConflicts(!showAllFactConflicts)}>{showAllFactConflicts ? 'Свернуть' : 'Все противоречия'}</button>}</div>{factConflictRows.map((item) => <FactConflictRow key={item.conflict_id || `${item.hotel_id}-${item.fact_a}-${item.fact_b}`} item={item} />)}{!factConflicts.length && <div className="empty-facts">Противоречия появятся, если ответ отеля расходится с расчётным фактом.</div>}</section>
    <CurationQueue city={panelCity} notify={notify} />
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">D34 · ПРОБЕЛЫ КАТАЛОГА</span><h2>Что спрашивают, а мы не знаем <span className="count-bubble">{journal.items.length}</span></h2></div><span className="sim-tag">ЖУРНАЛ ПОИСКА · {journal.days} ДН.</span></div>
      {journal.state === 'loading' && <div className="loading-inline">Загружаем журнал…</div>}
      {journal.state === 'error' && <div className="empty-facts">Журнал не загрузился: проверьте соединение с API.</div>}
      {journal.state === 'ready' && journal.items.map((item) => <div className="unknown-request-row" key={item.normalized}><div><strong>{item.label}</strong><span>{[item.unparsed ? 'не разобрано' : 'вне каталога', item.required_queries ? `обязательно — в ${item.required_queries}` : '', item.examples?.length ? `например: «${item.examples.join('», «')}»` : '', item.nearest_contracts?.length ? `ближе всего: ${item.nearest_contracts.map((id) => labels.features?.[id] || CONTRACTS[id] || id).join(', ')}` : ''].filter(Boolean).join(' · ')}</span></div><b title="Разных запросов">{item.queries}</b><button className="text-button" disabled={specBusy === 'draft'} onClick={() => prepareFeatureDraft(null, item.label)}>Сделать характеристику</button></div>)}
      {journal.state === 'ready' && !journal.items.length && <div className="review-placeholder"><div className="review-icon"><CircleHelp size={20} /></div><div><strong>Пока никто не спросил того, чего нет в каталоге</strong><p>Журнал пишет поиск по тексту гостя: части запроса вне каталога и неразобранные куски. Частота — число разных запросов.</p></div></div>}
    </section>
    <section className="admin-panel"><div className="workspace-section-head"><div><span className="section-kicker">D34 · ДОБАВИТЬ ПРИЗНАК</span><h2>Новая характеристика</h2></div><span className="sim-tag">ЧЕРНОВИК · ВКЛЮЧАЕТ ЧЕЛОВЕК</span></div>
      <form className="feature-draft-form" onSubmit={prepareFeatureDraft}><label htmlFor="feature-request">ОПИШИТЕ СВОИМИ СЛОВАМИ</label><div><input id="feature-request" value={featureText} onChange={(e) => setFeatureText(e.target.value)} placeholder="Например, доля застройки в радиусе 300 м" /><button className="primary-button" type="submit" disabled={specBusy === 'draft'}>{specBusy === 'draft' ? 'Готовим…' : 'Подготовить'} <ArrowRight size={14} /></button></div></form>
      {specDraft && <div className="feature-draft-preview"><Sparkles size={16} /><div>
        <strong>Черновик: «{specDraft.spec?.title || featureText}»{specDraft.by ? ` · ${specDraft.by === 'llm' ? 'модель' : 'правила'}` : ''}</strong>
        <span>Не сохранён и не включён: спецификацию правит и включает человек.</span>
        {specDraft.problems?.length > 0 && <ul className="spec-problems">{specDraft.problems.map((item) => <li key={item}>{item}</li>)}</ul>}
        {specDraft.warnings?.length > 0 && <ul className="spec-warnings">{specDraft.warnings.map((item) => <li key={item}>{item}</li>)}</ul>}
        <label htmlFor="spec-yaml">СПЕЦИФИКАЦИЯ (YAML)</label>
        <textarea id="spec-yaml" className="spec-yaml" rows="10" spellCheck="false" value={yamlText} onChange={(e) => { setYamlText(e.target.value); setTrial(null) }} />
        <button className="primary-button" disabled={specBusy === 'trial' || !yamlText.trim()} onClick={runTrial}>{specBusy === 'trial' ? 'Считаем…' : `Пробный расчёт · ${cityNames[panelCity] || panelCity}`}</button>
        {trial && <div className={`trial-result ${trial.ran ? '' : 'pending'}`}>{trial.ran
          ? <><strong>Посчитано у {Math.round((trial.coverage || 0) * 100)}% из {trial.hotels} отелей</strong><span>{Object.entries(trial.statuses || {}).map(([status, n]) => `${PROVENANCE[status] || status}: ${n}`).join(' · ')}</span>{trial.examples?.slice(0, 3).map((item) => <small key={item.hotel_id}>{item.name || item.hotel_id}: {item.value == null ? 'неизвестно' : formatFactValue({ value: item.value, unit: item.unit })}</small>)}</>
          : <><strong>Пробный расчёт не запущен</strong><span>{trial.reason}</span></>}{trial.warnings?.map((item) => <small key={item}>{item}</small>)}</div>}
        {trial?.ran && !activation && <button className="primary-button" disabled={specBusy === 'activate'} onClick={activateFeature}>{specBusy === 'activate' ? 'Считаем по всем отелям…' : 'Включить: посчитать для Москвы и Сочи'}</button>}
        {activation && <div className="trial-result"><strong>Включена: {activation.title}</strong><span>{Object.entries(activation.cities || {}).map(([city, c]) => `${cityNames[city] || city}: ${Math.round((c.coverage || 0) * 100)}% из ${c.hotels}`).join(' · ')}</span><small>Поиск уже понимает её по словам гостя; факты — с паспортом расчёта.</small></div>}
      </div></div>}
      {activeFeatures.length > 0 && <div className="active-features"><span className="section-kicker">ВКЛЮЧЕНЫ ТЕКСТОМ</span>{activeFeatures.map((f) => <div className="unknown-request-row" key={f.id}><div><strong>{f.title}</strong><span>{[f.synonyms?.length ? `слова гостя: «${f.synonyms.slice(0, 3).join('», «')}»` : '', ...Object.entries(f.cities || {}).map(([city, c]) => `${cityNames[city] || city}: ${Math.round((c.coverage || 0) * 100)}%`)].filter(Boolean).join(' · ')}</span></div></div>)}</div>}
    </section>
  </div>
}

function Metric({ label, value, hint }) { return <div className="metric-card"><span>{label}</span><strong>{value}</strong><small>{hint}</small></div> }

function parseMaybeJson(value) {
  if (typeof value !== 'string' || !/^[[{]/.test(value.trim())) return value
  try { return JSON.parse(value) } catch { return value }
}
function formatConflictValue(raw) {
  const value = parseMaybeJson(raw)
  if (value == null || value === '') return 'не указаны'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  const entries = Array.isArray(value) ? value.map((item, index) => [index, item]) : Object.entries(value)
  const shown = entries.filter(([, item]) => item != null && typeof item !== 'object').slice(0, 4).map(([key, item]) => Array.isArray(value) ? String(item) : `${key}: ${item}`)
  return shown.length ? shown.join(' · ') : 'не указаны'
}

function ConflictRow({ item }) {
  const labels = useLabels()
  return <div className="conflict-row"><div className="conflict-symbol">!</div><div className="conflict-copy"><strong>{item.name || item.hotel_id} · {featureName(item.feature_id, labels)}</strong><span>Страница Островка: {formatConflictValue(item.ota_value)} · Другие данные: {formatConflictValue(item.other_value)}</span><small>{item.resolution || item.classification}</small></div>{item.ota_url && <a href={item.ota_url} target="_blank" rel="noreferrer">Источник <ExternalLink size={13} /></a>}</div>
}

function FactConflictRow({ item }) {
  const labels = useLabels()
  return <div className="conflict-row fact-conflict-row"><div className="conflict-symbol">!</div><div className="conflict-copy"><strong>{item.hotel_id} · {featureName(item.feature_id, labels)}</strong><span>Наш расчёт ({item.fact_a}) расходится с ответом отеля ({item.fact_b})</span><small>{item.state}{item.updated_at ? ` · ${formatDateTime(item.updated_at)}` : ''}</small></div></div>
}

createRoot(document.getElementById('root')).render(<App />)
