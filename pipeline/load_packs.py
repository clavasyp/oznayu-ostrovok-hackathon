"""Загрузка пакетов городов в базу (сервис `init`, D6).

Повторный запуск с тем же пакетом ничего не делает.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from core.db import Building, City, FactRow, Hotel, HotelBuilding, PackLoad, Question, RoomType
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
    row["confidence_level"] = confidence.get("level")
    row["confidence_reason"] = confidence.get("reason")
    row["extra"] = {k: v for k, v in data.items() if k not in FACT_COLUMNS}
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
}
# Порядок важен: внешние ключи.
LOAD_ORDER = ["hotels", "room_types", "buildings", "hotel_buildings", "facts", "questions"]


def _batches(rows: Iterable[dict[str, Any]], size: int = BATCH) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


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
    counts = {}
    for kind in LOAD_ORDER:
        for f in (f for f in manifest.files if f.kind == kind):
            model, build = ROW_BUILDERS[kind]
            pk = [c.name for c in model.__table__.primary_key.columns]
            n = 0
            for batch in _batches(build(r) for r in iter_jsonl(pack_dir / f.path)):
                stmt = insert(model).values(batch)
                updatable = {c: stmt.excluded[c] for c in batch[0] if c not in pk}
                conn.execute(stmt.on_conflict_do_update(index_elements=pk, set_=updatable))
                n += len(batch)
            counts[kind] = counts.get(kind, 0) + n
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
