"""Живые характеристики на запросе (core/live.py): прогноз осадков C04, воздух E01, волны B03 —
Open-Meteo на день заезда. Сеть в тестах подменена: ответ API — по ячейкам 0,1°."""

from datetime import date, timedelta

import pytest

from agents.search import parse_query
from app.stub_store import StubStore
from core import live
from core.search import search
from core.verdict import GuestStatus

TODAY = date(2026, 10, 28)
DAY = TODAY + timedelta(days=3)  # в горизонте и воздуха (5 дней), и волн (8)
HOTELS = {
    h: {"hotel_id": h, "city_id": city, "name": h, "latitude": lat, "longitude": lon}
    for h, city, lat, lon in (
        ("dry", "sochi", 43.60, 39.70),
        ("wet", "sochi", 43.90, 39.30),
        ("msk", "moscow", 55.75, 37.60),
    )
}


@pytest.fixture(autouse=True)
def fresh_cache():
    live._CACHE.clear()
    yield
    live._CACHE.clear()


def api(values, calls=None):
    """Ответ Open-Meteo: на каждую ячейку запроса — значение по широте ячейки."""

    def get(url, params):
        if calls is not None:
            calls.append(url)
        lats = [float(x) for x in params["latitude"].split(",")]
        key = "daily" if "daily" in params else "hourly"
        var = params[key]
        return [{key: {var: values(lat)}} for lat in lats]

    return get


def test_forecast_by_cell_with_passport_and_cache():
    calls = []
    get = api(lambda lat: [10.0] if lat < 43.7 else [70.0], calls)
    hotels = [HOTELS["dry"], HOTELS["wet"]]
    facts = live.live_facts("C04", hotels, {"date": DAY.isoformat()}, today=TODAY, get=get)
    (dry,), (wet,) = facts["dry"], facts["wet"]
    assert dry.status == "computed" and dry.value == {"probability_percent": 10.0}
    assert wet.value == {"probability_percent": 70.0}
    assert (
        dry.sources[0].provider == "Open-Meteo Forecast API" and "open-meteo" in dry.sources[0].url
    )
    live.live_facts("C04", hotels, {"date": DAY.isoformat()}, today=TODAY, get=get)
    assert len(calls) == 1  # один запрос на все ячейки, повтор — из кеша


def test_pm25_is_daily_mean_and_sea_only_where_there_is_sea():
    get = api(lambda lat: [10.0, 20.0, None])
    (air,) = live.live_facts("E01", [HOTELS["dry"]], {"date": DAY.isoformat()}, today=TODAY,
                             get=get)["dry"]  # fmt: skip
    assert air.value == {"pm25": 15.0}  # среднее за сутки, пропуски часов не нули
    waves = live.live_facts("B03", [HOTELS["msk"]], {"date": DAY.isoformat()}, today=TODAY, get=get)
    assert waves["msk"][0].status == "not_applicable"


def test_out_of_range_and_network_failure_are_unknown_not_no():
    far = (TODAY + timedelta(days=40)).isoformat()
    (f,) = live.live_facts("C04", [HOTELS["dry"]], {"date": far}, today=TODAY, get=api(None))["dry"]
    assert f.status == "unknown" and f.unknown_reason == "forecast_out_of_range"

    def down(url, params):
        raise OSError("нет сети")

    (f,) = live.live_facts("C04", [HOTELS["dry"]], {"date": DAY.isoformat()}, today=TODAY,
                           get=down)["dry"]  # fmt: skip
    assert f.unknown_reason == "forecast_unavailable"
    assert live.live_facts("C04", [HOTELS["dry"]], {}, today=TODAY, get=down) == {}


def test_search_answers_no_rain_by_forecast(monkeypatch):
    monkeypatch.setattr(live, "fetch", api(lambda lat: [10.0] if lat < 43.7 else [70.0]))
    today = date.today()
    day = today + timedelta(days=3)
    month = ("января февраля марта апреля мая июня июля августа сентября октября ноября "
             "декабря").split()[day.month - 1]  # fmt: skip
    ast = parse_query(f"обязательно чтобы не было дождя {day.day} {month}", "sochi",
                      today=today, mode="rules").ast  # fmt: skip
    store = StubStore(hotel_by_id={h: HOTELS[h] for h in ("dry", "wet")})
    by = {r["hotel"]["hotel_id"]: r for r in search(store, ast)}
    assert by["dry"]["status"] == GuestStatus.fits
    assert by["wet"]["status"] == GuestStatus.not_fit
