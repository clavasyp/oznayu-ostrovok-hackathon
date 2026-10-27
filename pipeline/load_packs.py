"""Загрузка пакетов городов в базу (сервис `init`, D6).

Повторный запуск с тем же пакетом ничего не делает. Новая версия пакета заменяет данные города
целиком: строки старой версии удаляются. Факты, созданные уже в работе (ответы отельера,
проверка командой), не трогаются: у фактов из пакета в `extra.pack` записана его версия.
Так же переживают новую версию размещения категорий со слов отеля (у набора из пакета версия —
в `source.pack`) и названия корпусов со слов отеля — если категория, корпус и стены ещё есть.

Строки идут потоковым COPY (D41: холодный старт ≤ 10 минут). Перед загрузкой `clear_city`
убирает строки города, поэтому повтор ключа — ошибка пакета и откат всей загрузки города.
Через INSERT … ON CONFLICT остаются корпуса (контур — функцией PostGIS), а при новой версии
пакета ещё отели и кандидаты D33 (решение человека новым пакетом не перезаписывается).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection

from core.db import (
    Building,
    City,
    CityCoverage,
    CurationItem,
    Facade,
    FactRow,
    Hotel,
    HotelBuilding,
    HotelStation,
    OtaComparison,
    PackLoad,
    Place,
    Placement,
    PlacementSet,
    PlacementWindow,
    Question,
    RoomType,
    TransitStation,
    TransitTime,
)
from core.hotelier import replaces_placements
from core.packs import Manifest, PackError, iter_jsonl, sha256_file, verify
from core.placement_catalog import hotel_aliases, with_alias
from core.store_db import fact_to_row

BATCH = 2000
BULK_FK_THRESHOLD = 50000  # строк вида в пакете: FK проверяются одним проходом после COPY


@contextmanager
def bulk_relationships(conn: Connection, model, enabled: bool):
    """Массовый COPY (станции у отелей, места): FK проверяются одним проходом до COMMIT.

    PK и NOT NULL действуют всегда. Определения FK восстанавливаются и VALIDATE
    проверяет всю таблицу, включая прежние города. SAVEPOINT откатывает DDL даже
    при ошибке чтения файла. На время загрузки DDL держит блокировку таблицы;
    приложение жюри запускается после init. Без прав владельца — обычный COPY с FK.
    """
    if not enabled:
        yield
        return
    table_name = model.__table__.fullname  # public.place: не tiger.place образа PostGIS
    owner = conn.scalar(
        text(
            "SELECT pg_has_role(current_user, relowner, 'USAGE') "
            "FROM pg_class WHERE oid=to_regclass(:name)"
        ),
        {"name": table_name},
    )
    constraints = conn.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid), convalidated "
            "FROM pg_constraint WHERE conrelid=to_regclass(:name) "
            "AND contype='f' ORDER BY conname"
        ),
        {"name": table_name},
    ).all()
    if not owner or not constraints or not all(row[2] for row in constraints):
        yield
        return
    quote = conn.dialect.identifier_preparer.quote
    table = conn.dialect.identifier_preparer.format_table(model.__table__)
    with conn.begin_nested():
        for name, _, _ in constraints:
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {quote(name)}")
        yield
        for name, definition, _ in constraints:
            conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD CONSTRAINT {quote(name)} {definition} NOT VALID"
            )
        for name, _, _ in constraints:
            conn.exec_driver_sql(f"ALTER TABLE {table} VALIDATE CONSTRAINT {quote(name)}")


def _default(column) -> Any:
    """Значение по умолчанию модели (`default=dict` → `{}`), как у INSERT через SQLAlchemy."""
    default = column.default
    if default is None:
        return None
    if default.is_scalar:
        return default.arg
    return default.arg(None) if default.is_callable else None


def copy_rows(conn: Connection, model, rows: Iterable[dict], into: str | None = None) -> int:
    """Потоковый COPY в общей транзакции загрузки города.

    Колонки — все, кроме вычисляемых базой (`place.geog`). Нет поля — значение по умолчанию
    модели или NULL. JSONB — как у INSERT через SQLAlchemy: `None` в поле — JSON null.
    """
    from psycopg import sql
    from psycopg.copy import QueuedLibpqWriter
    from psycopg.types.json import Jsonb

    table = model.__table__
    columns = [c for c in table.columns if c.computed is None]
    names = [c.name for c in columns]
    defaults = {c.name: _default(c) for c in columns}
    jsonb = {c.name for c in columns if isinstance(c.type, JSONB)}
    target = (
        sql.Identifier(into) if into else sql.Identifier(*filter(None, [table.schema, table.name]))
    )
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        target, sql.SQL(", ").join(map(sql.Identifier, names))
    )
    n = 0
    # Очередь: строки готовятся, пока предыдущие уходят в базу (на фактах Сочи −25% времени).
    with (
        conn.connection.driver_connection.cursor() as cursor,
        cursor.copy(statement, writer=QueuedLibpqWriter(cursor)) as stream,
    ):
        for row in rows:
            values = []
            for name in names:
                if name in row:
                    value = row[name]
                    values.append(Jsonb(value) if name in jsonb else value)
                else:
                    value = defaults[name]
                    values.append(Jsonb(value) if name in jsonb and value is not None else value)
            stream.write_row(values)
            n += 1
    return n


def copy_facts(conn: Connection, rows: Iterable[dict], runtime_facts: bool) -> int:
    """Факты пакета — COPY прямо в `fact`. Если в городе есть факты из работы, совпавший id
    обновляет строку, как прежний INSERT … ON CONFLICT: COPY во временную таблицу и один
    INSERT … SELECT."""
    if not runtime_facts:
        return copy_rows(conn, FactRow, rows)
    conn.exec_driver_sql(
        "CREATE TEMP TABLE pack_fact (LIKE fact INCLUDING DEFAULTS) ON COMMIT DROP"
    )
    n = copy_rows(conn, FactRow, rows, into="pack_fact")
    names = [c.name for c in FactRow.__table__.columns]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in names if c != "fact_id")
    conn.exec_driver_sql(
        f"INSERT INTO fact ({', '.join(names)}) SELECT {', '.join(names)} FROM pack_fact "
        f"ON CONFLICT (fact_id) DO UPDATE SET {updates}"
    )
    conn.exec_driver_sql("DROP TABLE pack_fact")
    return n


def fact_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Факт пакета → строка таблицы fact (core.store_db.fact_to_row)."""
    return fact_to_row(raw)


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
        "subzone_id": raw.get("subzone_id"),
        "latitude": lat,
        "longitude": lon,
        "geom": func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326),
        "source": raw.get("source") or {},
    }


def hotel_copy_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Отель для COPY: точка — EWKT, разбирает сама база (те же float, что у ST_MakePoint)."""
    row = hotel_row(raw)
    return row | {"geom": f"SRID=4326;POINT({row['longitude']!r} {row['latitude']!r})"}


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
    "placement_sets": (PlacementSet, lambda r: r),
    "placements": (Placement, lambda r: r),
    "placement_windows": (PlacementWindow, lambda r: r),
    "buildings": (Building, building_row),
    "hotel_buildings": (HotelBuilding, lambda r: r),
    "facades": (Facade, lambda r: r),
    "places": (Place, lambda r: r),
    "transit_stations": (TransitStation, lambda r: r),
    "hotel_stations": (HotelStation, lambda r: r),
    "transit_times": (
        TransitTime,
        lambda r: {**r, "departure_at": datetime.fromisoformat(r["departure_at"])},
    ),
    "curation_items": (CurationItem, lambda r: r),
    "facts": (FactRow, fact_row),
    "questions": (Question, lambda r: r),
    "coverage": (CityCoverage, lambda r: r),
    "ota_comparisons": (OtaComparison, lambda r: r),
}
# Порядок важен: внешние ключи.
LOAD_ORDER = [
    "hotels",
    "room_types",
    "placement_sets",
    "placements",
    "placement_windows",
    "buildings",
    "hotel_buildings",
    "facades",
    "places",
    "transit_stations",
    "hotel_stations",
    "transit_times",
    "curation_items",
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
    conn.execute(delete(HotelStation).where(HotelStation.hotel_id.in_(hotels)))
    conn.execute(delete(TransitTime).where(TransitTime.city_id == city_id))
    conn.execute(delete(Place).where(Place.city_id == city_id))
    conn.execute(delete(TransitStation).where(TransitStation.city_id == city_id))
    building_ids = select(Building.id).where(Building.city_id == city_id).scalar_subquery()
    conn.execute(delete(Facade).where(Facade.building_id.in_(building_ids)))
    conn.execute(delete(FactRow).where(FactRow.city_id == city_id, FactRow.extra.has_key("pack")))
    # Вопросы из пакета пересоздаются; ответы на них в dev-базе при смене пакета сбрасываются.
    conn.execute(delete(Question).where(Question.hotel_id.in_(hotels)))
    conn.execute(delete(OtaComparison).where(OtaComparison.city_id == city_id))
    conn.execute(delete(CityCoverage).where(CityCoverage.city_id == city_id))
    conn.execute(delete(HotelBuilding).where(HotelBuilding.hotel_id.in_(hotels)))
    room_types = select(RoomType.id).where(RoomType.hotel_id.in_(hotels)).scalar_subquery()
    placements = select(Placement.id).where(Placement.room_type_id.in_(room_types))
    conn.execute(
        delete(PlacementWindow).where(
            PlacementWindow.placement_id.in_(placements.scalar_subquery())
        )
    )
    conn.execute(delete(Placement).where(Placement.room_type_id.in_(room_types)))
    conn.execute(delete(PlacementSet).where(PlacementSet.room_type_id.in_(room_types)))
    conn.execute(delete(RoomType).where(RoomType.hotel_id.in_(hotels)))
    conn.execute(delete(Building).where(Building.city_id == city_id))


def hotel_aliases_of_city(conn: Connection, city_id: str) -> dict[tuple[str, str], list[dict]]:
    """Названия корпусов со слов отеля (ответы о размещении) — до очистки города."""
    hotels = select(Hotel.id).where(Hotel.city_id == city_id).scalar_subquery()
    rows = conn.execute(
        select(HotelBuilding.hotel_id, HotelBuilding.building_id, HotelBuilding.evidence).where(
            HotelBuilding.hotel_id.in_(hotels)
        )
    )
    return {(h, b): kept for h, b, evidence in rows if (kept := hotel_aliases(evidence))}


def restore_hotel_aliases(conn: Connection, kept: dict[tuple[str, str], list[dict]]) -> int:
    """Вернуть названия на связи новой версии пакета. Связи «отель — корпус» больше нет —
    название не к чему привязать, оно не переносится на другой корпус."""
    restored = 0
    for (hotel_id, building_id), aliases in kept.items():
        where = (HotelBuilding.hotel_id == hotel_id, HotelBuilding.building_id == building_id)
        evidence = conn.execute(select(HotelBuilding.evidence).where(*where)).scalar_one_or_none()
        if evidence is None:
            continue
        for alias in aliases:
            evidence = with_alias(evidence, alias)
        conn.execute(update(HotelBuilding).where(*where).values(evidence=evidence))
        restored += len(aliases)
    return restored


def runtime_placements_of_city(conn: Connection, city_id: str) -> list[dict[str, Any]]:
    """Наборы размещений, созданные в работе (ответы отеля и имитатора, проверка командой), с
    размещениями и окнами — до очистки города. У набора из пакета в `source.pack` его версия."""
    hotels = select(Hotel.id).where(Hotel.city_id == city_id).scalar_subquery()
    rts = select(RoomType.id).where(RoomType.hotel_id.in_(hotels)).scalar_subquery()
    sets = conn.execute(
        select(PlacementSet.__table__, RoomType.hotel_id)
        .join(RoomType, RoomType.id == PlacementSet.room_type_id)
        .where(PlacementSet.room_type_id.in_(rts), ~PlacementSet.source.has_key("pack"))
    ).mappings()
    kept = {r["room_type_id"]: {"set": dict(r), "placements": []} for r in sets}
    if not kept:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for p in conn.execute(
        select(Placement.__table__).where(Placement.room_type_id.in_(list(kept)))
    ).mappings():
        by_id[p["id"]] = dict(p) | {"windows": []}
        kept[p["room_type_id"]]["placements"].append(by_id[p["id"]])
    if by_id:
        for w in conn.execute(
            select(PlacementWindow.__table__).where(PlacementWindow.placement_id.in_(list(by_id)))
        ).mappings():
            by_id[w["placement_id"]]["windows"].append(dict(w))
    return list(kept.values())


def restore_placements(conn: Connection, kept: list[dict[str, Any]]) -> tuple[int, int]:
    """Вернуть наборы из работы на новую версию пакета: (возвращено, отброшено).

    Набор возвращается, только если у отеля ещё есть категория, корпуса размещений и все стены
    окон (`seg-…` меняется вместе с геометрией). Набор пакета для той же категории он заменяет
    по тем же правилам, что и ответ (`replaces_placements`): имитатор не заменяет набор команды.
    """
    restored = dropped = 0
    for entry in kept:
        ps = {k: v for k, v in entry["set"].items() if k != "hotel_id"}
        rt_id, hotel_id = ps["room_type_id"], entry["set"]["hotel_id"]
        buildings = {p["building_id"] for p in entry["placements"]}
        walls = {
            (p["building_id"], w["facade_id"]) for p in entry["placements"] for w in p["windows"]
        }
        exists = conn.scalar(
            select(RoomType.id).where(RoomType.id == rt_id, RoomType.hotel_id == hotel_id)
        )
        linked = conn.scalar(
            select(func.count()).where(
                HotelBuilding.hotel_id == hotel_id, HotelBuilding.building_id.in_(buildings)
            )
        )
        found = 0
        for building_id in buildings:
            ids = [f for b, f in walls if b == building_id]
            found += conn.scalar(
                select(func.count()).where(
                    Facade.building_id == building_id, Facade.facade_id.in_(ids)
                )
            )
        if exists is None or linked != len(buildings) or found != len(walls):
            dropped += 1
            continue
        pack_status = conn.scalar(
            select(PlacementSet.status).where(PlacementSet.room_type_id == rt_id)
        )
        if pack_status is not None:
            if not replaces_placements(ps["status"], pack_status):
                dropped += 1
                continue
            old = select(Placement.id).where(Placement.room_type_id == rt_id).scalar_subquery()
            conn.execute(delete(PlacementWindow).where(PlacementWindow.placement_id.in_(old)))
            conn.execute(delete(Placement).where(Placement.room_type_id == rt_id))
            conn.execute(delete(PlacementSet).where(PlacementSet.room_type_id == rt_id))
        conn.execute(insert(PlacementSet).values(ps))
        placements = [{k: v for k, v in p.items() if k != "windows"} for p in entry["placements"]]
        if placements:
            conn.execute(insert(Placement).values(placements))
        windows = [w for p in entry["placements"] for w in p["windows"]]
        if windows:
            conn.execute(insert(PlacementWindow).values(windows))
        restored += 1
    return restored, dropped


def _mark(kind: str, row: dict[str, Any], pack_version: str) -> dict[str, Any]:
    if kind == "facts":
        row["extra"] = row.get("extra", {}) | {"pack": pack_version}
    elif kind == "placement_sets":  # набор из пакета: новая версия пакета его заменяет
        row["source"] = (row.get("source") or {}) | {"pack": pack_version}
    elif kind == "ota_comparisons":
        row["pack_version"] = pack_version
    return row


def load_pack(
    conn: Connection, pack_dir: Path, progress: Callable[[str], None] | None = None
) -> str:
    """Загрузить пакет города; `progress` получает строку после каждого вида файлов."""
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
    # Названия корпусов и размещения со слов отеля — до clear_city: он стирает связи «отель —
    # корпус» и все размещения категорий города.
    kept_aliases = hotel_aliases_of_city(conn, manifest.city_id)
    kept_placements = runtime_placements_of_city(conn, manifest.city_id)
    # И на чистой базе: после clear_city строк города нет, COPY не встретит повтор ключа.
    clear_city(conn, manifest.city_id)
    city = manifest.city_id

    def has(column) -> bool:  # остались ли у города строки после clear_city
        return (
            conn.scalar(select(column).where(column.table.c.city_id == city).limit(1)) is not None
        )

    runtime_facts = has(FactRow.fact_id)
    # INSERT … ON CONFLICT, если такие строки у города уже есть: отели обновляются (на них
    # ссылаются факты из работы), решение человека по кандидату D33 не перезаписывается.
    # Корпуса — всегда INSERT: контур из GeoJSON строит функция PostGIS. Остальное — COPY.
    upsert = {"hotels": has(Hotel.id), "buildings": True, "curation_items": has(CurationItem.id)}
    counts = {}
    hotel_ids: list[str] = []
    for kind in LOAD_ORDER:
        files = [f for f in manifest.files if f.kind == kind]
        if not files:
            continue
        started = time.monotonic()
        model, build = ROW_BUILDERS[kind]
        if kind == "hotels" and not upsert[kind]:
            build = hotel_copy_row
        bulk = sum(f.rows for f in files) > BULK_FK_THRESHOLD
        with bulk_relationships(conn, model, bulk and not upsert.get(kind)):
            for f in files:
                rows = (
                    _mark(kind, build(r), manifest.pack_version)
                    for r in iter_jsonl(pack_dir / f.path)
                )
                if upsert.get(kind):
                    n = _insert(conn, kind, model, rows, hotel_ids)
                elif kind == "facts":
                    n = copy_facts(conn, rows, runtime_facts)
                else:
                    if kind == "hotels":
                        rows = _collect_ids(rows, hotel_ids)
                    n = copy_rows(conn, model, rows)
                if n != f.rows:
                    raise PackError(f"{manifest.city_id}: число строк {f.path} не совпало")
                counts[kind] = counts.get(kind, 0) + n
        if progress:
            progress(
                f"{manifest.city_id}: {kind} {counts.get(kind, 0)} строк, "
                f"{time.monotonic() - started:.1f} с"
            )
    if kept_aliases:
        counts["hotel_aliases_kept"] = restore_hotel_aliases(conn, kept_aliases)
    if kept_placements:
        restored, dropped = restore_placements(conn, kept_placements)
        counts["placements_kept"], counts["placements_dropped"] = restored, dropped
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


def _collect_ids(rows: Iterable[dict[str, Any]], ids: list[str]) -> Iterator[dict[str, Any]]:
    for row in rows:
        ids.append(row["id"])
        yield row


def _insert(
    conn: Connection, kind: str, model, rows: Iterable[dict[str, Any]], hotel_ids: list[str]
) -> int:
    """INSERT … ON CONFLICT пачками: отели и корпуса обновляются, кандидаты D33 — нет."""
    pk = [c.name for c in model.__table__.primary_key.columns]
    n = 0
    for batch in _batches(rows):
        stmt = insert(model).values(batch)
        updatable = {c: stmt.excluded[c] for c in batch[0] if c not in pk}
        if kind == "curation_items":
            # Не перезаписывать решения и время проверки человека новым пакетом.
            conn.execute(stmt.on_conflict_do_nothing(index_elements=pk))
        elif updatable:
            conn.execute(stmt.on_conflict_do_update(index_elements=pk, set_=updatable))
        else:  # строка — только ключ
            conn.execute(stmt.on_conflict_do_nothing(index_elements=pk))
        n += len(batch)
        if kind == "hotels":
            hotel_ids.extend(r["id"] for r in batch)
    return n
