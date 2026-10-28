"""Срез Geofabrik: полные геометрии, общие ID узлов и отдельные запреты прохода.

Границы берутся из того же PBF: Москва relation/102269, Большой Сочи — union
relation/365233 и relation/11865346 (D28, review_gpt_v11.md). Графовый срез имеет
буфер, а каталог фильтруется по самому полигону. Сеть не используется.

Конверт Сочи захватывает Абхазию: места и станции за границей РФ (станция «Ԥсоу») отсекает
полигон субъектов РФ в конверте — Краснодарский край (relation/108082) и Адыгея
(relation/253256): `region.geojson`, `REGION_IDS`. `extract_places` — облегчённый срез только
для мест и станций D34 (`pipeline.nearby_tables`): те же записи, без зданий и графа, узлы — в
файловом индексе, а не в памяти.
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from collections import Counter
from pathlib import Path

import osmium
from shapely import from_wkb, make_valid
from shapely.geometry import Point, box, mapping
from shapely.ops import unary_union

from core.packs import sha256_file
from core.plugins import CityContext, DatasetRef, register_connector

VERSION = "osm-city.2"  # .2: подъёмники и аттракционы в срезе, граница субъекта (region.geojson)
PLACES_VERSION = "osm-places.1"
BOUNDARY_IDS = {"moscow": (102269,), "sochi": (365233, 11865346)}
# Субъекты РФ в конверте среза: места и станции вне их — за границей страны. Конверт Сочи —
# Краснодарский край и Адыгея (станции Хаджох, Тульская — Россия), южнее Псоу — Абхазия.
# У Москвы конверт весь в России, граница не нужна.
REGION_IDS = {"sochi": (108082, 253256)}
# Предварительный конверт; попадание каждой найденной границы проверяется ниже.
ENVELOPES = {"moscow": (36.65, 55.0, 38.3, 56.15), "sochi": (39.0, 43.2, 41.1, 44.6)}
KEYS = (
    "tourism",
    "building",
    "building:part",
    "highway",
    "amenity",
    "shop",
    "natural",
    "railway",
    "public_transport",
    "leisure",
    "healthcare",
    "entrance",
    "office",
    "lit",
    "barrier",
    "landuse",
    "man_made",
    "boundary",
    "place",
    "access",
    "foot",
    "locked",
    "foot:conditional",
    "access:conditional",
    "club",
    # Станции подъёмников и аттракционы — места словаря (ski_lift, theme_park). Без этих ключей
    # станция подъёмника без других меток в срез не попадала: в пакете Сочи было 54 из 114
    # станций в 1500 м от отелей, не было «Газпром (Альпика)», К8.
    "aerialway",
    "attraction",
)
# Срез мест: ключи, по которым `pipeline.nearby_tables` ищет места и станции.
PLACE_KEYS = (
    "amenity",
    "shop",
    "tourism",
    "leisure",
    "healthcare",
    "office",
    "aerialway",
    "attraction",
)
PLACE_FILTER_KEYS = (*PLACE_KEYS, "railway", "public_transport")


def place_candidate(tags) -> bool:
    """Объект нужен таблицам мест и станций: метка места или станция (`station_mode`)."""
    if any(tags.get(k) for k in PLACE_KEYS):
        return True
    return (
        tags.get("railway") in {"station", "halt", "tram_stop"}
        or tags.get("public_transport") == "station"
    )


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")


def _geometry(obj, tags: dict, factory, envelope, errors: Counter):
    """Геометрия объекта среза: узел — точка, собранный Area — мультиполигон, линия —
    LineString (замкнутая не-дорога приходит отдельно как Area). None — вне конверта или без
    геометрии; починка make_valid считается в `errors`."""
    if obj.is_node():
        if not obj.location.valid():
            errors["invalid_node_location"] += 1
            return None
        geom, typ, oid = Point(obj.location.lon, obj.location.lat), "node", obj.id
    elif obj.is_area():
        geom = from_wkb(factory.create_multipolygon(obj))
        typ, oid = ("way" if obj.from_way() else "relation"), obj.orig_id()
    else:
        if obj.is_closed() and "highway" not in tags:
            return None  # Не дублировать полигон линейной геометрией.
        geom = from_wkb(factory.create_linestring(obj))
        typ, oid = "way", obj.id
    if geom.is_empty or not geom.intersects(envelope):
        return None  # Проверяется вся геометрия, не первая вершина.
    if not geom.is_valid:
        geom = make_valid(geom)
        errors["geometry_repaired"] += 1
    if geom.is_empty:
        errors["empty_geometry"] += 1
        return None
    return geom, typ, oid


def _record(obj, tags: dict, geom, typ: str, oid: int, nodes) -> bytes:
    record = {
        "osm_type": typ,
        "osm_id": oid,
        "geometry_wkb": geom.wkb_hex,
        "tags": tags,
        "version": obj.version,
        "timestamp": str(obj.timestamp),
        "nodes": nodes,
    }
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _region_feature(obj, tags: dict, geom, oid: int) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(geom),
        "properties": {
            "osm_id": f"relation/{oid}",
            "name": tags.get("name"),
            "admin_level": tags.get("admin_level"),
            "version": obj.version,
            "timestamp": str(obj.timestamp),
        },
    }


def write_region(out: Path, city_id: str, regions: dict, digest: str, source_date) -> list[Path]:
    """`region.geojson` — субъект РФ, обрезанный по конверту среза: места и станции вне него
    за границей страны (Абхазия у Сочи). Нет границы у города — нет файла."""
    wanted = set(REGION_IDS.get(city_id, ()))
    if not wanted:
        return []
    missing = wanted - set(regions)
    if missing:
        raise ValueError(f"Не собрана граница субъекта OSM: {sorted(missing)}")
    from shapely.geometry import shape

    union = unary_union([shape(f["geometry"]) for f in regions.values()])
    clipped = union.intersection(box(*ENVELOPES[city_id]))
    path = out / "region.geojson"
    write_json(
        path,
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": None, "properties": f["properties"]}
                for _, f in sorted(regions.items())
            ],
            "city_id": city_id,
            "purpose": "places and stations outside this polygon are outside the country",
            "union_geometry": mapping(clipped),
            "clipped_to_envelope": list(ENVELOPES[city_id]),
            "source_sha256": digest,
            "source_date": source_date,
            "license": "ODbL-1.0",
        },
    )
    return [path]


def extract_city(pbf: Path, out: Path, city_id: str, *, source_url: str | None = None) -> dict:
    if city_id not in BOUNDARY_IDS:
        raise ValueError("Поддерживаются только Москва и Большой Сочи")
    pbf, out = pbf.resolve(), out.resolve()
    if not pbf.is_file():
        raise FileNotFoundError(pbf)
    digest = sha256_file(pbf)
    manifest_path = out / "manifest.json"
    if (old := _reuse(out, digest, VERSION)) is not None:
        return old
    out.mkdir(parents=True, exist_ok=True)
    bounds = ENVELOPES[city_id]
    envelope = box(*bounds)
    factory = osmium.geom.WKBFactory()
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations("flex_mem")
        .with_areas(osmium.filter.KeyFilter(*KEYS))
        .with_filter(osmium.filter.KeyFilter(*KEYS))
    )
    source_date = processor.header.get("osmosis_replication_timestamp")
    counts, errors = Counter(), Counter()
    boundaries, zones, places, regions = {}, [], [], {}
    started = time.monotonic()
    records_path, buildings_path = out / "records.jsonl.gz", out / "buildings.jsonl.gz"
    with (
        records_path.open("wb") as record_file,
        buildings_path.open("wb") as building_file,
        gzip.GzipFile(fileobj=record_file, mode="wb", mtime=0) as records,
        gzip.GzipFile(fileobj=building_file, mode="wb", mtime=0) as buildings,
    ):
        for obj in processor:
            if obj.is_relation():
                continue  # Для multipolygon ниже приходит собранный Area.
            try:
                tags = dict(obj.tags)
                nodes = None
                shaped = _geometry(obj, tags, factory, envelope, errors)
                if shaped is None:
                    continue
                geom, typ, oid = shaped
                if obj.is_way() and "highway" in tags:
                    nodes = [[n.ref, n.lon, n.lat] for n in obj.nodes]
                records.write(_record(obj, tags, geom, typ, oid, nodes))
                counts[typ] += 1
                if typ == "node" and any(
                    k in tags
                    for k in (
                        "barrier",
                        "access",
                        "foot",
                        "locked",
                        "foot:conditional",
                        "access:conditional",
                    )
                ):
                    counts["restriction_nodes"] += 1
                if typ == "relation" and oid in BOUNDARY_IDS[city_id] and obj.is_area():
                    if not envelope.covers(geom):
                        raise ValueError("Полигон города вышел за конверт среза")
                    boundaries[oid] = {
                        "type": "Feature",
                        "geometry": mapping(geom),
                        "properties": {
                            "osm_id": f"relation/{oid}",
                            "name": tags.get("name"),
                            "version": obj.version,
                            "timestamp": str(obj.timestamp),
                        },
                    }
                if typ == "relation" and oid in REGION_IDS.get(city_id, ()) and obj.is_area():
                    regions[oid] = _region_feature(obj, tags, geom, oid)
                if tags.get("boundary") == "administrative" and obj.is_area():
                    zones.append({"type": "Feature", "geometry": mapping(geom), "properties": tags})
                if tags.get("place") and tags.get("name"):
                    places.append(
                        {"type": "Feature", "geometry": mapping(geom), "properties": tags}
                    )
                if (tags.get("building") or tags.get("building:part")) and obj.is_area():
                    building = {
                        "id": f"{typ}/{oid}",
                        "city_id": city_id,
                        "geometry": mapping(geom),
                        "tags": tags,
                    }
                    buildings.write(
                        (json.dumps(building, ensure_ascii=False, sort_keys=True) + "\n").encode()
                    )
                    counts["buildings"] += 1
            except (osmium.InvalidLocationError, RuntimeError) as exc:
                errors[type(exc).__name__] += 1
    missing = set(BOUNDARY_IDS[city_id]) - set(boundaries)
    if missing:
        raise ValueError(f"Не собраны обязательные границы OSM: {sorted(missing)}")
    from shapely.geometry import shape

    union = unary_union([shape(f["geometry"]) for f in boundaries.values()])
    boundary_doc = {
        "type": "FeatureCollection",
        "features": list(boundaries.values()),
        "city_id": city_id,
        "union_geometry": mapping(union),
        "source_sha256": digest,
        "source_date": source_date,
        "source_url": source_url,
        "license": "ODbL-1.0",
    }
    write_json(out / "boundary.geojson", boundary_doc)
    write_json(
        out / "localities.geojson", {"type": "FeatureCollection", "features": places + zones}
    )
    region_files = write_region(out, city_id, regions, digest, source_date)
    files = [
        {"path": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size}
        for p in (
            records_path,
            buildings_path,
            out / "boundary.geojson",
            out / "localities.geojson",
            *region_files,
        )
    ]
    manifest = {
        "algorithm_version": VERSION,
        "city_id": city_id,
        "source_date": source_date,
        "source_sha256": digest,
        "source_url": source_url,
        "source_file": pbf.name,
        "bbox": list(bounds),
        "boundary_relation_ids": list(BOUNDARY_IDS[city_id]),
        "region_relation_ids": list(REGION_IDS.get(city_id, ())),
        "counts": dict(counts),
        "geometry_diagnostics": dict(errors),
        "files": files,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
        "node_policy": "Keep pure barrier/access/foot/locked nodes; retain shared OSM node IDs.",
        "extent_note": (
            "Graph uses a disclosed envelope; hotel membership uses the exact city union."
        ),
    }
    write_json(manifest_path, manifest)
    print(
        f"{city_id}: OSM {sum(v for k, v in counts.items() if k in {'node', 'way', 'relation'})} "
        f"записей, {counts['buildings']} зданий, {time.monotonic() - started:.1f} с",
        flush=True,
    )
    return manifest


def _reuse(out: Path, digest: str, version: str) -> dict | None:
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old.get("source_sha256") != digest or old.get("algorithm_version") != version:
            raise ValueError("В выходе уже другой срез OSM; укажите новый каталог")
        for entry in old["files"]:
            if sha256_file(out / entry["path"]) != entry["sha256"]:
                raise ValueError("Повреждён ранее созданный срез OSM")
        return old
    if out.exists() and any(out.iterdir()):
        raise ValueError("Для среза нужен новый или пустой каталог")
    return None


def extract_places(
    pbf: Path,
    out: Path,
    city_id: str,
    *,
    source_url: str | None = None,
    node_index: Path | None = None,
) -> dict:
    """Срез только для мест и станций D34: `records.jsonl.gz` тех же записей, что у
    `extract_city`, но лишь объектов `place_candidate` (места и станции), плюс `region.geojson`.
    Без зданий, графа и узлов дорог — пересборка мест пакета не требует полного среза.
    Координаты узлов — в файловом индексе `node_index` (sparse_file_array), а не в памяти:
    окружной PBF Центрального ФО — около 2 ГБ координат."""
    if city_id not in BOUNDARY_IDS:
        raise ValueError("Поддерживаются только Москва и Большой Сочи")
    pbf, out = pbf.resolve(), out.resolve()
    if not pbf.is_file():
        raise FileNotFoundError(pbf)
    digest = sha256_file(pbf)
    if (old := _reuse(out, digest, PLACES_VERSION)) is not None:
        return old
    out.mkdir(parents=True, exist_ok=True)
    envelope = box(*ENVELOPES[city_id])
    factory = osmium.geom.WKBFactory()
    keys = PLACE_FILTER_KEYS + (("boundary",) if REGION_IDS.get(city_id) else ())
    index = node_index or out.with_name(out.name + ".nodes")
    if index.exists():
        raise ValueError(f"Файл индекса узлов уже есть: {index}")
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations(f"sparse_file_array,{index}")
        .with_areas(osmium.filter.KeyFilter(*keys))
        .with_filter(osmium.filter.KeyFilter(*keys))
    )
    source_date = processor.header.get("osmosis_replication_timestamp")
    counts, errors, regions = Counter(), Counter(), {}
    wanted_regions = set(REGION_IDS.get(city_id, ()))
    started = time.monotonic()
    records_path = out / "records.jsonl.gz"
    try:
        with (
            records_path.open("wb") as record_file,
            gzip.GzipFile(fileobj=record_file, mode="wb", mtime=0) as records,
        ):
            for obj in processor:
                if obj.is_relation():
                    continue  # Для multipolygon приходит собранный Area.
                region = obj.is_area() and not obj.from_way() and obj.orig_id() in wanted_regions
                if not region and not place_candidate(obj.tags):
                    continue
                try:
                    tags = dict(obj.tags)
                    shaped = _geometry(obj, tags, factory, envelope, errors)
                    if shaped is None:
                        continue
                    geom, typ, oid = shaped
                    if region:
                        regions[oid] = _region_feature(obj, tags, geom, oid)
                        if not place_candidate(tags):
                            continue
                    records.write(_record(obj, tags, geom, typ, oid, None))
                    counts[typ] += 1
                except (osmium.InvalidLocationError, RuntimeError) as exc:
                    errors[type(exc).__name__] += 1
    finally:
        index.unlink(missing_ok=True)
    files = [records_path, *write_region(out, city_id, regions, digest, source_date)]
    manifest = {
        "algorithm_version": PLACES_VERSION,
        "city_id": city_id,
        "source_date": source_date,
        "source_sha256": digest,
        "source_url": source_url,
        "source_file": pbf.name,
        "bbox": list(ENVELOPES[city_id]),
        "region_relation_ids": list(REGION_IDS.get(city_id, ())),
        "filter_keys": list(keys),
        "selection": "connectors.osm_pbf.place_candidate: places and stations; "
        "buildings and graph are not extracted",
        "counts": dict(counts),
        "geometry_diagnostics": dict(errors),
        "files": [
            {"path": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size} for p in files
        ],
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
    }
    write_json(out / "manifest.json", manifest)
    print(
        f"{city_id}: срез мест OSM — {sum(counts.values())} записей, "
        f"{time.monotonic() - started:.1f} с",
        flush=True,
    )
    return manifest


@register_connector
class OsmPbfConnector:
    id = "osm_pbf"
    source_id = "osm_pbf"
    layers = ("buildings", "paths", "poi", "boundary", "restriction_nodes")

    def available(self, ctx: CityContext) -> bool:
        ref = ctx.datasets.get(self.id)
        return bool(ref and ref.path.is_file())

    def fetch(self, ctx: CityContext, workdir: Path) -> list[DatasetRef]:
        if not self.available(ctx):
            raise FileNotFoundError("Нужна локальная выгрузка Geofabrik в ctx.datasets['osm_pbf']")
        ref = ctx.datasets[self.id]
        manifest = extract_city(ref.path, workdir, ctx.city_id, source_url=ref.url)
        return [
            DatasetRef(
                id=f"osm_city:{ctx.city_id}",
                path=workdir,
                provider="OpenStreetMap/Geofabrik",
                version=manifest["source_date"],
                sha256=sha256_file(workdir / "manifest.json"),
                license="ODbL-1.0",
                url=ref.url,
            )
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--city", required=True, choices=tuple(BOUNDARY_IDS))
    parser.add_argument("--source-url")
    parser.add_argument(
        "--places-only",
        action="store_true",
        help="только места и станции D34 (extract_places): без зданий и графа",
    )
    args = parser.parse_args()
    extract = extract_places if args.places_only else extract_city
    manifest = extract(args.pbf, args.out, args.city, source_url=args.source_url)
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
