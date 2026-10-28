"""Свежесть данных (бонус трека «актуализация»): когда значение получено и когда его пора
обновить, и чем.

- Дата значения: расчёт (`calculated_at`) → наблюдение (`observed_at`) → дата сборки пакета
  (`extra.date_kind = pack_built_at`, её проставляет загрузка пакета у фактов без своей даты).
- Срок обновления: свой `refresh_after` факта (климат — через год) или срок по виду данных.
- Как обновить: рецепт → команда конвейера; ответ отеля — повторный вопрос в кабинете.

Устаревшее значение не выбрасывается: ответ по нему остаётся, но помечен «пора обновить».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from core.facts import Fact

# Срок годности по виду данных, если у факта нет своего `refresh_after`, дней.
MAX_AGE_DAYS = {
    "ostrovok_content": 30,  # страница отеля меняется: цены, удобства, категории
    "observed": 90,
    "computed": 180,  # расчёт по картам и рельефу: город меняется медленнее
    "estimate": 180,
    "hotelier": 180,
    "simulated_hotelier": 180,
    "curated": 365,
}
# Чем обновлять: начало `algorithm_version` или `recipe_id` → шаг конвейера (packs/FORMAT.md).
REFRESH = [
    ("live-open-meteo", "на запросе: прогноз Open-Meteo, кеш на час"),
    ("ostrovok-amenities", "новый снимок Островка → python -m pipeline.build_hotel_amenities"),
    ("ostrovok-site", "новый снимок Островка → python -m pipeline.build_packs"),
    ("view-sun", "python -m pipeline.build_view_sun --city <город>"),
    ("route-poi", "свежая выгрузка OSM → python -m pipeline.calculate_city"),
    ("env.", "python -m pipeline.fetch_era5 и python -m pipeline.build_environment"),
    ("geo.place_count", "POST /api/specs/activate — пересчёт характеристики по местам"),
    ("transit-2gis", "на запросе: 2ГИС, кеш на диске"),
    ("heritage-wikidata", "свежая выгрузка Wikidata → python -m pipeline.build_heritage"),
    ("building-year-osm", "свежий срез OSM → python -m pipeline.build_building_years"),
    ("beach-surface-osm", "свежий срез Geofabrik → python -m pipeline.build_beach_surface"),
    ("construction-osm", "свежий срез Geofabrik → python -m pipeline.build_construction"),
]
HOTELIER = {"hotelier", "simulated_hotelier"}


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def dated(fact: Fact) -> tuple[datetime | None, str]:
    """Дата значения и что она значит: `calculated`, `observed` или `pack_built_at`."""
    if fact.calculated_at:
        kind = getattr(fact, "date_kind", None) or "calculated"
        return _dt(fact.calculated_at), kind
    if fact.observed_at:
        return _dt(fact.observed_at), "observed"
    return None, "none"


def due(fact: Fact) -> datetime | None:
    """Когда пора обновить: свой `refresh_after` или дата значения + срок по виду данных."""
    own = _dt(fact.refresh_after) or _dt(fact.valid_to)
    if own:
        return own
    when, _ = dated(fact)
    status = getattr(fact.status, "value", fact.status)
    days = MAX_AGE_DAYS.get(str(status))
    return when + timedelta(days=days) if when and days else None


def is_stale(fact: Fact, now: datetime | None = None) -> bool:
    when = due(fact)
    return bool(when and when <= (now or datetime.now(UTC)))


def hint_for(status: str, recipe: str) -> str:
    """Как обновить значение — по рецепту; ответ отеля — повторным вопросом."""
    if status in HOTELIER:
        return "повторный вопрос отелю в кабинете отельера"
    if status == "curated":
        return "повторная проверка командой (D33)"
    return next((hint for prefix, hint in REFRESH if prefix in recipe), "пересборка пакета города")


def refresh_hint(fact: Fact) -> str:
    status = str(getattr(fact.status, "value", fact.status))
    return hint_for(status, f"{fact.algorithm_version or ''} {fact.recipe_id or ''}")


def summary(facts: list[Fact], as_of: datetime) -> list[dict[str, Any]]:
    """Сводка свежести по городу без базы (заглушки): как `DbStore.freshness`."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for f in facts:
        status = str(getattr(f.status, "value", f.status))
        if status == "unknown":
            continue
        recipe = f.algorithm_version or f.recipe_id or ""
        g = groups.setdefault((f.feature_id, status, recipe), {"n": 0, "dates": [], "dues": [],
                                                               "stale": 0})  # fmt: skip
        when, until = dated(f)[0], due(f)
        g["n"] += 1
        g["dates"] += [when] if when else []
        g["dues"] += [until] if until else []
        g["stale"] += bool(until and until <= as_of)
    return [
        row(
            fid,
            status,
            recipe,
            g["n"],
            min(g["dates"], default=None),
            min(g["dues"], default=None),
            g["stale"],
        )  # fmt: skip
        for (fid, status, recipe), g in groups.items()
    ]


def row(
    feature_id: str,
    status: str,
    recipe: str,
    n: int,
    oldest: datetime | None,
    next_due: datetime | None,
    stale: int,
) -> dict[str, Any]:
    return {
        "feature_id": feature_id,
        "status": status,
        "recipe": recipe,
        "n": n,
        "oldest": oldest.isoformat() if oldest else None,
        "next_due": next_due.isoformat() if next_due else None,
        "stale": stale,
        "refresh_hint": hint_for(status, recipe),
    }


def freshness(fact: Fact, now: datetime | None = None) -> dict[str, Any]:
    """Для паспорта факта: дата, что она значит, срок обновления, устарело ли и чем обновить."""
    when, kind = dated(fact)
    until = due(fact)
    return {
        "date": when.isoformat() if when else None,
        "date_kind": kind,
        "refresh_due": until.isoformat() if until else None,
        "stale": bool(until and until <= (now or datetime.now(UTC))),
        "refresh_hint": refresh_hint(fact),
    }
