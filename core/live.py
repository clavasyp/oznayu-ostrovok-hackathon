"""Живые характеристики на запросе (D34): прогноз осадков C04, качество воздуха E01 (PM2.5) и
высота волн B03 — Open-Meteo на день заезда гостя.

В пакете их нет: прогноз меняется за часы, а горизонт — 16 дней. Агент сам выбирает источник по
датам гостя: дальше горизонта — климат месяца из пакета (C01–C03), ближе — этот прогноз. Один
запрос на характеристику и город: отели группируются по ячейкам 0,1°. Ответ кешируется на час.
Сеть недоступна — честное «неизвестно» с причиной, а не догадка (D5).
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.facts import Fact, make_fact_id

VERSION = "live-open-meteo.1"
CELL_DEG = 0.1
TTL_S = 3600
TIMEOUT_S = 8
LICENSE = "CC BY 4.0; бесплатный API — только некоммерческий прототип (D22)"


# Характеристика → адрес API, переменная, поле значения, единица, как свести часы к суткам.
LIVE: dict[str, dict[str, Any]] = {
    "C04": {
        "url": "https://api.open-meteo.com/v1/forecast",
        "provider": "Open-Meteo Forecast API",
        "daily": "precipitation_probability_max",
        "field": "probability_percent",
        "unit": "%",
        "days": 16,  # горизонт прогноза: дальше API отвечает пустыми рядами
    },
    "E01": {
        "url": "https://air-quality-api.open-meteo.com/v1/air-quality",
        "provider": "Open-Meteo Air Quality API (CAMS)",
        "hourly": "pm2_5",
        "field": "pm25",
        "unit": "µg/m3",
        "days": 5,
    },
    "B03": {
        "url": "https://marine-api.open-meteo.com/v1/marine",
        "provider": "Open-Meteo Marine API",
        "daily": "wave_height_max",
        "field": "hs_m",
        "unit": "m",
        "days": 8,
        "extra": {"cell_selection": "sea"},  # ближайшая морская ячейка, а не суша у отеля
    },
}
SEA_CITIES = frozenset({"sochi", "istanbul", "dubai", "kaliningrad", "spb"})

_CACHE: dict[tuple, tuple[float, dict]] = {}
# Последние живые факты по id: объяснение и паспорт факта находят их, как факты пакета.
_RECENT: OrderedDict[str, Fact] = OrderedDict()
RECENT_MAX = 50_000


def recent(fact_id: str) -> Fact | None:
    return _RECENT.get(fact_id)


def remember(fact: Fact) -> Fact:
    """Живой факт (прогноз, транспорт) — чтобы паспорт и объяснение нашли его по id."""
    return _remember(fact)


def _remember(fact: Fact) -> Fact:
    _RECENT[fact.fact_id] = fact
    _RECENT.move_to_end(fact.fact_id)
    while len(_RECENT) > RECENT_MAX:
        _RECENT.popitem(last=False)
    return fact


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return round(round(lat / CELL_DEG) * CELL_DEG, 4), round(round(lon / CELL_DEG) * CELL_DEG, 4)


def _tls() -> ssl.SSLContext:
    """Сертификаты системы (образ Debian); где их нет (Python.org на Mac) — certifi, если есть."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _url(url: str, params: dict[str, Any]) -> str:
    return f"{url}?{urllib.parse.urlencode(params)}"


def fetch(url: str, params: dict[str, Any]) -> list[dict]:
    """Один запрос на все ячейки; ответ — список по ячейкам в том же порядке. Стандартная
    библиотека: у образа приложения нет httpx (SDK моделей тянут свой httpx2)."""
    with urllib.request.urlopen(_url(url, params), timeout=TIMEOUT_S, context=_tls()) as r:
        data = json.loads(r.read())
    return data if isinstance(data, list) else [data]


def _cell_values(
    fid: str, cells: list[tuple[float, float]], day: date, get: Callable
) -> tuple[dict[tuple[float, float], float | None], str]:
    spec = LIVE[fid]
    params: dict[str, Any] = {
        "latitude": ",".join(str(c[0]) for c in cells),
        "longitude": ",".join(str(c[1]) for c in cells),
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "timezone": "Europe/Moscow",
        **spec.get("extra", {}),
    }
    key = "daily" if "daily" in spec else "hourly"
    params[key] = spec[key]
    url = _url(spec["url"], params)
    cache_key = (fid, day, tuple(cells))
    hit = _CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < TTL_S:
        return hit[1], url
    values: dict[tuple[float, float], float | None] = {}
    for cell, block in zip(cells, get(spec["url"], params), strict=True):
        series = [v for v in (block.get(key) or {}).get(spec[key]) or [] if v is not None]
        if not series:
            values[cell] = None
        elif key == "daily":
            values[cell] = float(series[0])
        else:  # PM2.5 по часам — среднее за сутки, как в ориентире ВОЗ
            values[cell] = round(sum(series) / len(series), 1)
    _CACHE[cache_key] = (time.monotonic(), values)
    return values, url


def live_facts(
    fid: str,
    hotels: Iterable[dict[str, Any]],
    params: dict[str, Any],
    *,
    today: date | None = None,
    get: Callable[[str, dict[str, Any]], list[dict]] | None = None,
) -> dict[str, list[Fact]]:
    """Факты одной живой характеристики для отелей: «отель → [факт]», как `Store.facts_for`."""
    get = get or fetch
    raw_day = params.get("date")
    if not raw_day:
        return {}  # без даты прогноз не на что смотреть — разбор уже спросил даты
    day = date.fromisoformat(str(raw_day)[:10])
    today = today or datetime.now(UTC).date()
    hotels = [h for h in hotels if h.get("latitude") is not None]
    now = datetime.now(UTC)
    reason: str | None = None
    if not 0 <= (day - today).days < LIVE[fid]["days"]:
        reason = "forecast_out_of_range"
    values: dict[tuple[float, float], float | None] = {}
    url = LIVE[fid]["url"]
    if reason is None:
        cells = sorted({_cell(h["latitude"], h["longitude"]) for h in hotels})
        try:
            values, url = _cell_values(fid, cells, day, get)
        except (OSError, ValueError, KeyError):  # сеть, HTTP-ошибка (URLError), битый ответ
            reason = "forecast_unavailable"
    out: dict[str, list[Fact]] = defaultdict(list)
    for h in hotels:
        if fid == "B03" and h.get("city_id") not in SEA_CITIES:
            out[h["hotel_id"]].append(
                _remember(_fact(fid, h, params, now, url, None, "not_applicable"))
            )
            continue
        value = values.get(_cell(h["latitude"], h["longitude"])) if reason is None else None
        why = reason or (None if value is not None else "forecast_cell_empty")
        out[h["hotel_id"]].append(_remember(_fact(fid, h, params, now, url, value, why)))
    return dict(out)


def _fact(
    fid: str,
    hotel: dict[str, Any],
    params: dict[str, Any],
    now: datetime,
    url: str,
    value: float | None,
    reason: str | None,
) -> Fact:
    spec = LIVE[fid]
    hid = hotel["hotel_id"]
    status = (
        "not_applicable"
        if reason == "not_applicable"
        else "unknown"
        if value is None
        else "computed"
    )
    return Fact(
        fact_id=make_fact_id(hid, f"hotel:{hid}", fid, dict(params), f"{VERSION}:{now:%Y%m%dT%H}"),
        hotel_id=hid,
        city_id=hotel.get("city_id") or "",
        subject_level="hotel",
        subject_id=f"hotel:{hid}",
        feature_id=fid,
        params=dict(params),
        value={spec["field"]: value} if value is not None else None,
        unit=spec["unit"],
        status=status,
        unknown_reason=reason if status == "unknown" else None,
        sources=[
            {
                "provider": spec["provider"],
                "url": url,
                "license": LICENSE,
                "fetched_at": now.isoformat(),
            }
        ],  # fmt: skip
        recipe_id=f"{fid}-OPEN-METEO-LIVE",
        algorithm_version=VERSION,
        confidence={
            "level": "medium",
            "reason": "прогноз модели на день; до даты может измениться",
        },
        calculated_at=now,
        refresh_after=(now + timedelta(seconds=TTL_S)).isoformat(),
        scope_note=f"Прогноз на {params.get('date')} для ячейки {CELL_DEG}° вокруг отеля.",
    )
