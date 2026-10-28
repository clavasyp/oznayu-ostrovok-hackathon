"""Характеристики каталога «место рядом» — по местам пакета на запросе (D41, D34).

Выгул собак P03, коворкинг R04, детская неотложка F02, аптека L01, еда L02, супермаркет L03,
ветклиника P04. Пар «отель → место» в пакете нет: лучшее подходящее место в радиусе условия
ищет PostGIS (`Store.nearest_places` с рангом места), как у «рядом место типа X».

Ранг места (меньше — лучше), его считает код по меткам OSM места:
- `YES` (0) — тип подтверждён метками; если гость назвал момент — по часам OSM открыто;
- `HOURS_UNKNOWN` (1) — тип подходит, но часов нет, они не разбираются, зависят от дня (момент
  без даты — проверяем каждый день недели) или бар открыт, а кормят ли — не указано;
- `UNCONFIRMED` (2) — детская больница без подтверждённой неотложки (F02);
- `CLOSED` (3) — по часам в нужный момент закрыто.
Частный доступ (`access=private|no`), явное `emergency=no` у F02 — не место для гостя: ранга нет.

Момент — параметр условия: `arrival_local` (дата и час) или `time_local` (только час: «ночью» —
02:00, проверяем семь ближайших дней). Часовой пояс — города (`Store.cities`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from calculators.poi_hours import opening_state

YES, HOURS_UNKNOWN, UNCONFIRMED, CLOSED = 0, 1, 2, 3
NO_ACCESS = frozenset({"private", "no"})
KITCHEN_KEYS = ("opening_hours:kitchen", "kitchen:opening_hours", "food_service_hours")
FOOD_VENUES = frozenset({"restaurant", "cafe", "fast_food", "food_court"})
WEEK = 7  # момент без даты — каждый из семи ближайших дней
COUNTRY = {"minsk": "BY", "istanbul": "TR", "dubai": "AE"}  # праздники PH в часах; иначе RU


def _tokens(value: str | None) -> set[str]:
    return {v.strip() for v in (value or "").split(";") if v.strip()}


CHILD = re.compile(r"детск|педиатр")
EMERGENCY = re.compile(r"неотложн|скор\w*\s+(?:медицинск\w+\s+)?помощ|травмпункт")
HOSPITAL = frozenset({"hospital"})


def child_emergency(tags: dict[str, Any]) -> int | None:
    """Детская неотложная помощь (F02): `YES` — подтверждена метками OSM (`emergency=yes` и
    педиатрия в `healthcare:speciality`) или названием («детский травмпункт», «НИИ неотложной
    детской хирургии»); `UNCONFIRMED` — детская больница без отметки о неотложке; иначе — не она.
    Поликлиника без подтверждения — не неотложка: её нет в ответе."""
    if tags.get("emergency") == "no":
        return None
    name = " ".join(str(tags.get(k) or "") for k in ("name", "official_name")).lower()
    name = name.replace("ё", "е")
    specialities = _tokens(tags.get("healthcare:speciality"))
    paediatric = any(t.startswith(("paediatric", "pediatric")) for t in specialities)
    child = paediatric or bool(CHILD.search(name))
    if not child:
        return None
    if (tags.get("emergency") == "yes" and paediatric) or EMERGENCY.search(name):
        return YES
    hospital = HOSPITAL & (_tokens(tags.get("amenity")) | _tokens(tags.get("healthcare")))
    return UNCONFIRMED if hospital else None


@dataclass(frozen=True)
class PlaceFeature:
    category: str  # категория мест пакета: `core.geo_store.CATALOG_PLACES` или словарь мест
    kind: Callable[[dict[str, Any]], int | None] | None = None  # тип: YES / UNCONFIRMED / нет
    food: bool = False  # еда: часы кухни, бар без часов кухни не подтверждает еду


# F02 ищем среди больниц словаря мест: подтверждение неотложки — по меткам и названию на запросе,
# категории F02 пакета (только метки) для этого мало.
FEATURES: dict[str, PlaceFeature] = {
    "L01": PlaceFeature("L01"),
    "L02": PlaceFeature("L02", food=True),
    "L03": PlaceFeature("L03"),
    "P03": PlaceFeature("P03"),
    "P04": PlaceFeature("P04"),
    "R04": PlaceFeature("R04"),
    "F02": PlaceFeature("hospital", kind=child_emergency),
}


def moment_of(params: dict[str, Any]) -> dict[str, str] | None:
    """Момент из параметров условия: дата и час или только час; без них часы не проверяем."""
    if params.get("arrival_local"):
        return {"arrival_local": str(params["arrival_local"])}
    if params.get("time_local"):
        return {"time_local": str(params["time_local"])}
    return None


def _instants(moment: dict[str, str], today: date) -> list[str]:
    if "arrival_local" in moment:
        return [moment["arrival_local"]]
    time = moment["time_local"]
    return [f"{(today + timedelta(days=i)).isoformat()}T{time}:00" for i in range(WEEK)]


def hours_at(
    tags: dict[str, Any],
    moment: dict[str, str],
    timezone: str | None,
    country: str = "RU",
    *,
    food: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Открыто ли место в момент гостя по часам OSM (`calculators/poi_hours.py`).

    `state`: open | closed | unknown | varies (момент без даты: открыто не каждый день) |
    no_food (бар открыт, а часов кухни и отметки «есть еда» нет)."""
    out: dict[str, Any] = {"moment": moment, "timezone": timezone}
    kitchen = food and any(tags.get(k) for k in KITCHEN_KEYS)
    if not timezone:
        return out | {"state": "unknown", "opening_hours": None, "reason": "city_timezone_unknown"}
    states = [
        opening_state(tags, t, timezone=timezone, country=country, kitchen=kitchen)
        for t in _instants(moment, today or date.today())
    ]
    seen = {s["state"] for s in states}
    if "unknown" in seen:
        state = "unknown"
    elif len(seen) == 1:
        state = seen.pop()
    else:
        state = "varies"
    serves = _tokens(tags.get("amenity")) & FOOD_VENUES or tags.get("food") == "yes"
    if food and state == "open" and not kitchen and not serves:
        state = "no_food"
    out |= {"state": state, "opening_hours": states[0].get("expression"), "kitchen": kitchen}
    if state == "unknown":
        out["reason"] = states[0].get("reason")
    return out


def classify(
    spec: PlaceFeature,
    place: dict[str, Any],
    moment: dict[str, str] | None,
    timezones: dict[str, str],
    today: date | None = None,
) -> tuple[int, dict[str, Any] | None] | None:
    """Ранг места и проверка часов (если есть момент); None — место не подходит вовсе."""
    tags = place.get("tags") or {}
    if tags.get("access") in NO_ACCESS:
        return None
    # Название места — и из строки места, если в метках его нет (2ГИС, заглушки).
    kind = spec.kind({"name": place.get("name")} | tags) if spec.kind else YES
    if kind is None:
        return None
    hours = None
    if moment:
        city = place.get("city_id") or ""
        hours = hours_at(
            tags, moment, timezones.get(city), COUNTRY.get(city, "RU"), food=spec.food, today=today
        )
    if hours and hours["state"] == "closed":
        return CLOSED, hours
    if kind != YES:
        return kind, hours
    if hours and hours["state"] != "open":
        return HOURS_UNKNOWN, hours
    return YES, hours


def ranker(
    spec: PlaceFeature,
    moment: dict[str, str] | None,
    timezones: dict[str, str],
    today: date | None = None,
) -> Callable[[dict[str, Any]], int | None]:
    """Ранг места для `Store.nearest_places`: меньше — лучше, None — не подходит."""

    def rank(place: dict[str, Any]) -> int | None:
        found = classify(spec, place, moment, timezones, today)
        return None if found is None else found[0]

    return rank


# Причина «неизвестно» по рангу и состоянию часов лучшего места.
HOURS_REASON = {
    "varies": "place_hours_vary",
    "no_food": "place_kitchen_unknown",
    "unknown": "place_hours_unknown",
}


def reason_of(rank: int, hours: dict[str, Any] | None) -> str | None:
    if rank == CLOSED:
        return "place_closed_at_moment"
    if rank == UNCONFIRMED:
        return "place_emergency_unconfirmed"
    if rank == HOURS_UNKNOWN:
        return HOURS_REASON.get((hours or {}).get("state", "unknown"), "place_hours_unknown")
    return None
