"""Время на общественном транспорте на запросе (D37): из аэропорта T01, с вокзала T02, есть ли
маршрут в нужное время T03 — 2ГИС Public Transport API.

Демо-ключ — 1 000 запросов на сервис в месяц, поэтому считаем только первую страницу выдачи
(`PAGE_MAX` отелей) и только при ключе `TWOGIS_API_KEY`; ответы кешируются на диске, расход
ведёт клиент (`connectors.twogis`, общий журнал на ключ). Без ключа, вне лимита или без сети —
факта нет, и пункт остаётся «неизвестно» с честной причиной (D5). Ключ у жюри свой.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.facts import Fact, make_fact_id
from core.live import remember

TRANSIT = frozenset({"T01", "T02", "T03"})
VERSION = "transit-2gis.1"
PAGE_MAX = 10
MONTHLY_UNITS = 300  # своя граница внутри демо-квоты 2ГИС: остальное — на проверки и показ
# Сколько вариантов маршрута просить: лучший по версии 2ГИС. Больше — длиннее ответ, а с
# иностранного адреса 2ГИС обрывает ответы больше ~20 КБ (проверено 29.10); в России — без
# ограничения, можно поднять `TWOGIS_MAX_ROUTES`.
MAX_ROUTES = int(os.environ.get("TWOGIS_MAX_ROUTES") or 1)
CACHE = Path(os.environ.get("TWOGIS_CACHE", "/tmp/twogis_cache"))
TZ = {"sochi": "Europe/Moscow", "moscow": "Europe/Moscow"}
# Откуда ехать: аэропорт или вокзал, названный в запросе, иначе главный в городе.
ORIGINS: dict[str, dict[str, list[tuple[str, str, float, float]]]] = {
    "sochi": {
        "T01": [("сочи|адлер", "аэропорт Сочи", 43.4499, 39.9566)],
        "T02": [
            ("адлер", "вокзал Адлер", 43.4370, 39.9250),
            ("сочи", "вокзал Сочи", 43.5879, 39.7225),
        ],
    },
    "moscow": {
        "T01": [
            ("шереметьев", "аэропорт Шереметьево", 55.9726, 37.4146),
            ("домодедов", "аэропорт Домодедово", 55.4088, 37.9063),
            ("внуков", "аэропорт Внуково", 55.6033, 37.2921),
        ],
        "T02": [
            (
                "ленинградск|казанск|ярославск|комсомольск",
                "площадь трёх вокзалов",
                55.7764,
                37.6553,
            ),
            ("киевск", "Киевский вокзал", 55.7431, 37.5664),
            ("курск", "Курский вокзал", 55.7579, 37.6611),
            ("павелецк", "Павелецкий вокзал", 55.7297, 37.6400),
            ("белорусск", "Белорусский вокзал", 55.7767, 37.5817),
        ],
    },
}


def enabled() -> bool:
    return bool(os.environ.get("TWOGIS_API_KEY"))


def origin(city: str, fid: str, text: str) -> tuple[str, float, float]:
    """Точка отправления: названная в запросе или первая (главная) в городе."""
    options = ORIGINS.get(city, {}).get("T01" if fid == "T03" else fid, [])
    low = text.lower().replace("ё", "е")
    for words, name, lat, lon in options:
        if re.search(words, low):
            return name, lat, lon
    _, name, lat, lon = options[0]
    return name, lat, lon


def departure(city: str, params: dict[str, Any], today: date | None = None) -> datetime:
    """Время отправления: из запроса (`arrival_local`), иначе завтра в полдень по городу."""
    tz = ZoneInfo(TZ.get(city, "Europe/Moscow"))
    raw = params.get("arrival_local")
    if raw:
        when = datetime.fromisoformat(str(raw))
        return when if when.tzinfo else when.replace(tzinfo=tz)
    day = (today or datetime.now(tz).date()) + timedelta(days=1)
    return datetime.combine(day, time(12, 0), tz)


def transit_facts(
    fid: str,
    hotels: list[dict[str, Any]],
    params: dict[str, Any],
    text: str,
    *,
    client: Any = None,
) -> dict[str, list[Fact]]:
    """Факты одной транспортной характеристики для отелей страницы: «отель → [факт]»."""
    from connectors.twogis import TwoGISClient, TwoGISError

    hotels = [h for h in hotels if h.get("latitude") is not None][:PAGE_MAX]
    if not hotels:
        return {}
    city = hotels[0].get("city_id") or ""
    if city not in ORIGINS:
        return {}
    client = client or TwoGISClient(CACHE, limit=MONTHLY_UNITS)
    name, lat, lon = origin(city, fid, text)
    start = departure(city, params)
    out: dict[str, list[Fact]] = {}
    for h in hotels:
        body = {
            "source": {"point": {"lat": lat, "lon": lon}},
            "target": {"point": {"lat": h["latitude"], "lon": h["longitude"]}},
            "transport": ["bus", "trolleybus", "tram", "shuttle_bus", "metro", "suburban_train"],
            "start_time": int(start.timestamp()),
            "enable_schedule": True,
            "locale": "ru",
            "max_result_count": MAX_ROUTES,
        }
        try:
            record = client.request("transit", body)
        except (TwoGISError, OSError, ValueError):
            continue  # без ключа, вне лимита или без сети — факта нет, пункт «неизвестно»
        response = record["response"] if isinstance(record["response"], list) else []
        routes = [
            r
            for r in response
            if not r.get("pedestrian") and r.get("transport") and r.get("total_duration")
        ]
        best = min(routes, key=lambda r: r["total_duration"], default=None)
        value: dict[str, Any] = {"route_exists": best is not None, "origin": name}
        if best:
            value |= {
                "duration_min": round(best["total_duration"] / 60),
                "transfers": int(best.get("transfer_count") or 0),
            }
        out[h["hotel_id"]] = [remember(_fact(fid, h, params, value, record["source"], start))]
    return out


def _fact(
    fid: str,
    hotel: dict[str, Any],
    params: dict[str, Any],
    value: dict[str, Any],
    source: dict[str, Any],
    start: datetime,
) -> Fact:
    hid = hotel["hotel_id"]
    when = datetime.now(UTC)
    return Fact(
        fact_id=make_fact_id(
            hid, f"hotel:{hid}", fid, dict(params), f"{VERSION}:{start:%Y%m%dT%H%M}"
        ),
        hotel_id=hid,
        city_id=hotel.get("city_id") or "",
        subject_level="hotel",
        subject_id=f"hotel:{hid}",
        feature_id=fid,
        params=dict(params),
        value=value,
        status="computed",
        sources=[source | {"departure_at": start.isoformat()}],
        recipe_id=f"{fid}-2GIS-PT",
        algorithm_version=VERSION,
        confidence={
            "level": "medium",
            "reason": f"Маршрут 2ГИС по расписанию от «{value['origin']}» на {start:%d.%m %H:%M}; "
            "пробки и задержки не учтены.",
        },
        calculated_at=when,
        refresh_after=(when + timedelta(hours=6)).isoformat(),
        scope_note=f"От «{value['origin']}» до отеля на общественном транспорте, лучший маршрут.",
    )


def trip_routes(
    target: dict[str, Any], hotels: list[dict[str, Any]], *, client: Any = None
) -> dict[str, dict[str, Any]]:
    """«Доехать до X» (D37): лучший маршрут 2ГИС от отеля до цели — общественный транспорт или
    пешком, если так быстрее. Только для первой страницы (`PAGE_MAX`) и только с ключом; нет
    маршрута, ключа или сети — отеля нет в ответе, пункт остаётся оценкой по прямой."""
    from connectors.twogis import TwoGISClient, TwoGISError

    hotels = [h for h in hotels if h.get("latitude") is not None][:PAGE_MAX]
    if not hotels or target.get("latitude") is None:
        return {}
    client = client or TwoGISClient(CACHE, limit=MONTHLY_UNITS)
    start = departure(hotels[0].get("city_id") or "", {})
    out: dict[str, dict[str, Any]] = {}
    for h in hotels:
        body = {
            "source": {"point": {"lat": h["latitude"], "lon": h["longitude"]}},
            "target": {"point": {"lat": target["latitude"], "lon": target["longitude"]}},
            "transport": ["bus", "trolleybus", "tram", "shuttle_bus", "metro", "suburban_train"],
            "start_time": int(start.timestamp()),
            "enable_schedule": True,
            "locale": "ru",
            "max_result_count": MAX_ROUTES,
        }
        try:
            record = client.request("transit", body)
        except (TwoGISError, OSError, ValueError):
            continue
        response = record["response"] if isinstance(record["response"], list) else []
        routes = [r for r in response if r.get("total_duration")]
        best = min(routes, key=lambda r: r["total_duration"], default=None)
        if best is None:
            continue
        out[h["hotel_id"]] = {
            "duration_min": round(best["total_duration"] / 60),
            "transfers": int(best.get("transfer_count") or 0),
            "walk_only": bool(best.get("pedestrian")) or not best.get("transport"),
            "departure_at": start.isoformat(),
            "provider": "2ГИС, на запросе",
            "url": (record.get("source") or {}).get("url"),
        }
    return out
