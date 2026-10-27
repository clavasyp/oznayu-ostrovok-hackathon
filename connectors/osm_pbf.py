"""Срез Geofabrik: полные геометрии, общие ID узлов и отдельные запреты прохода.

Границы берутся из того же PBF: Москва relation/102269, Большой Сочи — union
relation/365233 и relation/11865346 (D28, review_gpt_v11.md). Графовый срез имеет
буфер, а каталог фильтруется по самому полигону. Сеть не используется.
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

VERSION = "osm-city.1"
BOUNDARY_IDS = {"moscow": (102269,), "sochi": (365233, 11865346)}
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
)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")


def extract_city(pbf: Path, out: Path, city_id: str, *, source_url: str | None = None) -> dict:
    if city_id not in BOUNDARY_IDS:
        raise ValueError("Поддерживаются только Москва и Большой Сочи")
    pbf, out = pbf.resolve(), out.resolve()
    if not pbf.is_file():
        raise FileNotFoundError(pbf)
    digest = sha256_file(pbf)
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old.get("source_sha256") != digest or old.get("algorithm_version") != VERSION:
            raise ValueError("В выходе уже другой срез OSM; укажите новый каталог")
        for entry in old["files"]:
            if sha256_file(out / entry["path"]) != entry["sha256"]:
                raise ValueError("Повреждён ранее созданный срез OSM")
        return old
    if out.exists() and any(out.iterdir()):
        raise ValueError("Для среза нужен новый или пустой каталог")
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
    boundaries, zones, places = {}, [], []
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
                if obj.is_node():
                    if not obj.location.valid():
                        errors["invalid_node_location"] += 1
                        continue
                    geom, typ, oid = Point(obj.location.lon, obj.location.lat), "node", obj.id
                elif obj.is_area():
                    geom = from_wkb(factory.create_multipolygon(obj))
                    typ, oid = ("way" if obj.from_way() else "relation"), obj.orig_id()
                else:
                    if obj.is_closed() and "highway" not in tags:
                        continue  # Не дублировать полигон линейной геометрией.
                    geom = from_wkb(factory.create_linestring(obj))
                    typ, oid = "way", obj.id
                if geom.is_empty or not geom.intersects(envelope):
                    continue  # Проверяется вся геометрия, не первая вершина.
                if not geom.is_valid:
                    geom = make_valid(geom)
                    errors["geometry_repaired"] += 1
                if geom.is_empty:
                    errors["empty_geometry"] += 1
                    continue
                if obj.is_way() and "highway" in tags:
                    nodes = [[n.ref, n.lon, n.lat] for n in obj.nodes]
                record = {
                    "osm_type": typ,
                    "osm_id": oid,
                    "geometry_wkb": geom.wkb_hex,
                    "tags": tags,
                    "version": obj.version,
                    "timestamp": str(obj.timestamp),
                    "nodes": nodes,
                }
                records.write(
                    (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
                )
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
    files = [
        {"path": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size}
        for p in (
            records_path,
            buildings_path,
            out / "boundary.geojson",
            out / "localities.geojson",
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
    args = parser.parse_args()
    manifest = extract_city(args.pbf, args.out, args.city, source_url=args.source_url)
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
