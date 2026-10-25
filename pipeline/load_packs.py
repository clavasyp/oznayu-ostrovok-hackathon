"""Загрузка пакетов городов в базу (сервис `init`, D6).

Повторный запуск с тем же пакетом ничего не делает. Новая версия пакета заменяет данные города
целиком: строки старой версии удаляются. Факты, созданные уже в работе (ответы отельера,
проверка командой), не трогаются: у фактов из пакета в `extra.pack` записана его версия.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.db import (
    Building,
    City,
    CityCoverage,
    FactRow,
    Hotel,
    HotelBuilding,
    OtaComparison,
    PackLoad,
    Question,
    RoomType,
)
from core.facts import Fact
from core.packs import Manifest, iter_jsonl, sha256_file, verify

BATCH = 2000
FACT_COLUMNS = {c.name for c in FactRow.__table__.columns}


def fact_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Факт пакета → строка таблицы fact. Валидация по контракту; лишние поля v9 → extra."""
    fact = Fact.model_validate(raw)
    data = fact.model_dump(mode="json")
    confidence = data.pop("confidence", None) or {}
    row = {k: v for k, v in data.items() if k in FACT_COLUMNS}
    row["confidence_level"] = confidence.pop("level", None)
    row["confidence_reason"] = confidence.pop("reason", None)
    row["extra"] = {k: v for k, v in data.items() if k not in FACT_COLUMNS}
    if confidence:  # прочие поля уверенности, если калькулятор их дал
        row["extra"]["confidence"] = confidence
    return row


def hotel_row(raw: dict[str, Any]) -> dict[str, Any]:
    lon, lat = float(raw["longitude"]), float(raw["latitude"])
    return {
        "id": str(raw["id"]),
        "city_id": raw["city_id"],
        "name": raw["name"],
        "address": raw.get("address"),
        "kind": raw.get("kind"),
        "stars": raw.get("stars"),
        "subzone": raw.get("subzone"),
        "latitude": lat,
        "longitude": lon,
        "geom": func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326),
        "source": raw.get("source") or {},
    }


def building_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "city_id": raw["city_id"],
        "geom": func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(raw["geometry"])), 4326),
        "tags": raw.get("tags") or {},
    }


ROW_BUILDERS = {
    "hotels": (Hotel, hotel_row),
    "room_types": (RoomType, lambda r: r),
    "buildings": (Building, building_row),
    "hotel_buildings": (HotelBuilding, lambda r: r),
    "facts": (FactRow, fact_row),
    "questions": (Question, lambda r: r),
    "coverage": (CityCoverage, lambda r: r),
    "ota_comparisons": (OtaComparison, lambda r: r),
}
# Порядок важен: внешние ключи.
LOAD_ORDER = [
    "hotels",
    "room_types",
    "buildings",
    "hotel_buildings",
    "facts",
    "questions",
    "coverage",
    "ota_comparisons",
]


def _batches(rows: Iterable[dict[str, Any]], size: int = BATCH) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def clear_city(conn: Connection, city_id: str) -> None:
    """Убрать строки прежней версии пакета города. Отели — после вставки новых (см. load_pack)."""
    hotels = select(Hotel.id).where(Hotel.city_id == city_id).scalar_subquery()
    conn.execute(delete(FactRow).where(FactRow.city_id == city_id, FactRow.extra.has_key("pack")))
    # Вопросы из пакета пересоздаются; ответы на них в dev-базе при смене пакета сбрасываются.
    conn.execute(delete(Question).where(Question.hotel_id.in_(hotels)))
    conn.execute(delete(OtaComparison).where(OtaComparison.city_id == city_id))
    conn.execute(delete(CityCoverage).where(CityCoverage.city_id == city_id))
    conn.execute(delete(HotelBuilding).where(HotelBuilding.hotel_id.in_(hotels)))
    conn.execute(delete(RoomType).where(RoomType.hotel_id.in_(hotels)))
    conn.execute(delete(Building).where(Building.city_id == city_id))


def _mark(kind: str, row: dict[str, Any], pack_version: str) -> dict[str, Any]:
    if kind == "facts":
        row["extra"] = row.get("extra", {}) | {"pack": pack_version}
    elif kind == "ota_comparisons":
        row["pack_version"] = pack_version
    return row


def load_pack(conn: Connection, pack_dir: Path) -> str:
    manifest: Manifest = verify(pack_dir)
    manifest_sha = sha256_file(pack_dir / "manifest.json")
    loaded = conn.execute(
        select(PackLoad.manifest_sha256).where(PackLoad.city_id == manifest.city_id)
    ).scalar_one_or_none()
    if loaded == manifest_sha:
        return f"{manifest.city_id}: пакет {manifest.pack_version} уже загружен"

    conn.execute(
        insert(City)
        .values(
            id=manifest.city_id,
            name=manifest.city_name,
            timezone=manifest.timezone,
            depth=manifest.depth,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "name": manifest.city_name,
                "timezone": manifest.timezone,
                "depth": manifest.depth,
            },
        )
    )
    if loaded is not None:
        clear_city(conn, manifest.city_id)
    counts = {}
    hotel_ids: list[str] = []
    for kind in LOAD_ORDER:
        for f in (f for f in manifest.files if f.kind == kind):
            model, build = ROW_BUILDERS[kind]
            pk = [c.name for c in model.__table__.primary_key.columns]
            n = 0
            rows = (
                _mark(kind, build(r), manifest.pack_version) for r in iter_jsonl(pack_dir / f.path)
            )
            for batch in _batches(rows):
                stmt = insert(model).values(batch)
                updatable = {c: stmt.excluded[c] for c in batch[0] if c not in pk}
                conn.execute(stmt.on_conflict_do_update(index_elements=pk, set_=updatable))
                n += len(batch)
                if kind == "hotels":
                    hotel_ids.extend(r["id"] for r in batch)
            counts[kind] = counts.get(kind, 0) + n
    if loaded is not None and hotel_ids:
        # Отели, которых нет в новой версии пакета (их дочерние строки уже удалены в clear_city).
        conn.execute(
            delete(Hotel).where(Hotel.city_id == manifest.city_id, Hotel.id.not_in(hotel_ids))
        )
    conn.execute(
        insert(PackLoad)
        .values(
            city_id=manifest.city_id,
            pack_version=manifest.pack_version,
            manifest_sha256=manifest_sha,
        )
        .on_conflict_do_update(
            index_elements=["city_id"],
            set_={"pack_version": manifest.pack_version, "manifest_sha256": manifest_sha},
        )
    )
    return f"{manifest.city_id}: загружено {counts}"
