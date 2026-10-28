"""Слой «стройка рядом» (X03) для отелей Москвы и Сочи — по стройкам на карте OSM, оценкой.

    python -m pipeline.build_construction --pbf moscow=<срез Geofabrik ЦФО> --pbf sochi=<ЮФО>

Стройка — полигон `landuse=construction` или `building=construction` из того же среза Geofabrik,
что у пакета. Значение — расстояние от точки отеля до края ближайшей стройки; стройки на карте
ближе `SEARCH_M` нет — «не ближе `SEARCH_M` м» (`bound: lower`). Это оценка (D18): карта
отстаёт от жизни — новую стройку могут ещё не отметить, законченную — не снять; дат работ в OSM
обычно нет. «Без стройки рядом» она поднимает или опускает в выдаче, обязательное условие не
закрывает. Срок свежести — `REFRESH_DAYS`: стройки меняются быстрее зданий.
"""

from __future__ import annotations

import argparse
import math
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.facts import Fact, make_fact_id
from core.packs import PackSource, add_layer, iter_jsonl, read_manifest, sha256_file
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/construction.jsonl.gz"
VERSION = "construction-osm.1"
SEARCH_M = 1000
PAD_DEG = 0.02  # стройка у края рамки отелей
REFRESH_DAYS = 90


def _metric(lat0: float) -> tuple[float, float]:
    """Плоские метры около широты города (множители долготы и широты): на километр — доли метра."""
    return 111_320 * math.cos(math.radians(lat0)), 110_540.0


def extract(pbf: Path, hotels: list[dict], index: Path) -> tuple[list[dict], str | None]:
    """Стройки в рамке отелей: id, название, геометрия (WKB в градусах)."""
    import osmium
    from shapely import from_wkb, make_valid
    from shapely.geometry import box

    lats = [float(h["latitude"]) for h in hotels]
    lons = [float(h["longitude"]) for h in hotels]
    frame = box(min(lons) - PAD_DEG, min(lats) - PAD_DEG, max(lons) + PAD_DEG, max(lats) + PAD_DEG)
    factory = osmium.geom.WKBFactory()
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations(f"sparse_file_array,{index}")
        .with_areas(
            osmium.filter.TagFilter(("landuse", "construction"), ("building", "construction"))
        )
        .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
    )
    date = processor.header.get("osmosis_replication_timestamp")
    out = []
    try:
        for obj in processor:
            tags = dict(obj.tags)
            if tags.get("landuse") != "construction" and tags.get("building") != "construction":
                continue
            try:
                geom = from_wkb(factory.create_multipolygon(obj))
            except RuntimeError:
                continue
            geom = geom if geom.is_valid else make_valid(geom)
            if not geom.intersects(frame):
                continue
            kind = "way" if obj.from_way() else "relation"
            out.append({"id": f"{kind}/{obj.orig_id()}", "name": tags.get("name"),
                        "timestamp": obj.timestamp.isoformat(), "geometry": geom})  # fmt: skip
    finally:
        index.unlink(missing_ok=True)
    return out, date


def construction_facts(
    city: str, hotels: list[dict], sites: list[dict], meta: dict[str, Any], built_at: datetime
) -> list[dict]:
    from shapely import STRtree, transform
    from shapely.geometry import Point

    lat0 = sum(float(h["latitude"]) for h in hotels) / max(len(hotels), 1)
    kx, ky = _metric(lat0)
    shapes = [transform(s["geometry"], lambda xy: xy * (kx, ky)) for s in sites]
    tree = STRtree(shapes) if shapes else None
    rows = []
    for h in hotels:
        hid = str(h["id"])
        point = Point(float(h["longitude"]) * kx, float(h["latitude"]) * ky)
        near = None
        if tree is not None:
            hit = tree.query_nearest(point, max_distance=SEARCH_M, return_distance=True)
            if len(hit[0]):
                i, d = int(hit[0][0]), float(hit[1][0])
                near = (sites[i], d)
        source: dict[str, Any] = {
            "provider": "OpenStreetMap",
            "license": "ODbL-1.0",
            "dataset_sha256": meta["pbf_sha256"],
            "dataset_version": meta["source_date"],
        }
        if near:
            site, d = near
            value = {"distance_m": round(d), "site_id": site["id"], "site_name": site["name"]}
            source |= {"url": f"https://www.openstreetmap.org/{site['id']}",
                       "observed_at": site["timestamp"]}  # fmt: skip
            note = f"Ближайшая стройка на карте OSM — в {round(d)} м" + (
                f" («{site['name']}»)." if site["name"] else "."
            )
        else:
            value = {"distance_m": SEARCH_M, "bound": "lower"}
            note = f"Строек на карте OSM ближе {SEARCH_M} м нет."
        fact = Fact(
            fact_id=make_fact_id(hid, f"hotel:{hid}", "X03", {}, VERSION),
            hotel_id=hid,
            city_id=city,
            subject_level="hotel",
            subject_id=f"hotel:{hid}",
            feature_id="X03",
            value=value,
            unit="m",
            status="estimate",
            sources=[source],
            recipe_id="X03-OSM-construction-distance",
            algorithm_version=VERSION,
            confidence={
                "level": "low",
                "reason": "Стройки — полигоны landuse/building=construction в OSM; карта отстаёт "
                "от жизни, дат работ обычно нет.",
            },
            calculated_at=built_at,
            refresh_after=(built_at + timedelta(days=REFRESH_DAYS)).isoformat(),
            scope_note=note,
            hard_condition_eligible=False,
        )
        rows.append(fact_row(fact))
    return rows


def build(city: str, pbf: Path, packs: Path = PACKS) -> list[dict]:
    pack = packs / city
    manifest = read_manifest(pack)
    hotels = list(iter_jsonl(pack / "data/hotels.jsonl.gz"))
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:  # индекс узлов среза — около 2 ГБ, не в репозитории
        sites, date = extract(pbf, hotels, Path(tmp) / "nodes.idx")
    meta = {"pbf_sha256": sha256_file(pbf), "source_date": date}
    rows = construction_facts(city, hotels, sites, meta, manifest.built_at or datetime.now(UTC))
    rows.sort(key=lambda r: (r["hotel_id"], r["fact_id"]))
    write_jsonl_gz(pack / LAYER, rows)
    source = PackSource(
        id="construction:osm",
        provider="OpenStreetMap/Geofabrik (стройки)",
        release=date,
        sha256=meta["pbf_sha256"],
        license="ODbL-1.0",
        commercial_ok=True,
    )
    add_layer(pack, LAYER, "facts", len(rows), [source])
    bands = Counter(
        "≤300 м" if r["value"]["distance_m"] <= 300 and "bound" not in r["value"]
        else "300–1000 м" if "bound" not in r["value"] else "нет ближе 1 км"
        for r in rows
    )  # fmt: skip
    print(f"{city}: строек на карте в рамке {len(sites)}; отелей {len(rows)}: {dict(bands)}; "
          f"{round(time.monotonic() - started)} с")  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pbf", action="append", required=True, help="город=путь к срезу PBF")
    args = ap.parse_args()
    for item in args.pbf:
        city, path = item.split("=", 1)
        build(city, Path(path))


if __name__ == "__main__":
    main()
