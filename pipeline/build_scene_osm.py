"""Сцена для вида и солнца из среза OSM: здания, их части и деревья вокруг корпусов отелей.

    python -m pipeline.build_scene_osm --pbf <срез Geofabrik> --pack packs/moscow \\
        --out <scene.jsonl>

Формат — как у сцены Сочи из расчёта v9 (`osm_sochi_extended.jsonl`): строка на объект OSM с
`osm_type`, `osm_id`, `tags`, `geometry_wkb` (hex), `version`, `timestamp`. Берём то, что
`calculators.view_sun.load_scene` читает как препятствия: `building` / `building:part`
(контуры), `natural=tree` (точки), `natural=tree_row` (линии), `natural=wood` и
`landuse=forest` (контуры). Только в радиусе `BUILDINGS_M` от корпусов отелей пакета — дальше
расчёт берёт поверхность GLO-30. Рядом пишется `<out>.meta.json`: срез, его sha256 и дата.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from calculators.view_sun import BUILDINGS_M
from core.packs import sha256_file
from pipeline.build_view_sun import pack_inputs


def _envelopes(pack: Path, radius_m: float):
    """Рамки корпусов отелей с запасом `radius_m` (градусы) и их дерево для быстрой проверки."""
    import shapely
    from shapely.geometry import box, shape

    buildings, _, links, _ = pack_inputs(pack)
    linked = {x["building_id"] for x in links}
    boxes = []
    for b in buildings:
        if b["id"] not in linked or not b.get("geometry"):
            continue
        x0, y0, x1, y1 = shape(b["geometry"]).bounds
        dy = radius_m / 110_540
        dx = radius_m / (111_320 * math.cos(math.radians((y0 + y1) / 2)))
        boxes.append(box(x0 - dx, y0 - dy, x1 + dx, y1 + dy))
    union = shapely.union_all(boxes)
    return shapely.STRtree(list(getattr(union, "geoms", [union]))), union.bounds, len(linked)


def _iso(ts) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")


def build(pbf: Path, pack: Path, out: Path, radius_m: float = BUILDINGS_M) -> dict:
    import osmium
    from shapely import from_wkb
    from shapely.geometry import box

    tree, (w, s, e, n), linked = _envelopes(pack, radius_m)
    frame = box(w, s, e, n)
    factory = osmium.geom.WKBFactory()
    counts: Counter = Counter()
    started = time.monotonic()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with tempfile.TemporaryDirectory() as folder, tmp.open("w", encoding="utf-8") as f:
        processor = (
            osmium.FileProcessor(pbf)
            .with_locations(f"sparse_file_array,{Path(folder) / 'nodes.idx'}")
            .with_areas(osmium.filter.KeyFilter("building", "building:part", "natural", "landuse"))
            .with_filter(osmium.filter.KeyFilter("building", "building:part", "natural", "landuse"))
        )
        source_date = processor.header.get("osmosis_replication_timestamp")
        for obj in processor:
            tags = dict(obj.tags)
            try:
                if obj.is_area():
                    building = any(tags.get(k, "no") != "no" for k in ("building", "building:part"))
                    woods = tags.get("natural") == "wood" or tags.get("landuse") == "forest"
                    if not building and not woods:
                        continue
                    wkb = factory.create_multipolygon(obj)
                    kind, oid = (
                        ("way", obj.orig_id()) if obj.from_way() else ("relation", obj.orig_id())
                    )
                elif obj.is_way():
                    if tags.get("natural") != "tree_row":
                        continue
                    wkb, kind, oid = factory.create_linestring(obj), "way", obj.id
                elif obj.is_node():
                    if tags.get("natural") != "tree":
                        continue
                    wkb, kind, oid = factory.create_point(obj), "node", obj.id
                else:
                    continue
            except (RuntimeError, osmium.InvalidLocationError):
                counts["bad_geometry"] += 1
                continue
            geom = from_wkb(wkb)
            if not geom.intersects(frame) or not len(tree.query(geom, predicate="intersects")):
                continue
            row = {"osm_type": kind, "osm_id": oid, "geometry_wkb": geom.wkb_hex, "tags": tags,
                   "version": obj.version, "timestamp": _iso(obj.timestamp),
                   "nodes": None}  # fmt: skip
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts["building" if "building" in tags or "building:part" in tags else "trees"] += 1
    tmp.replace(out)
    meta = {
        "pbf": pbf.name,
        "pbf_sha256": sha256_file(pbf),
        "source_date": source_date,
        "radius_m": radius_m,
        "hotel_buildings": linked,
        "objects": dict(counts),
        "scene_sha256": sha256_file(out),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - started),
    }
    out.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pbf", type=Path, required=True)
    ap.add_argument("--pack", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--radius", type=float, default=BUILDINGS_M)
    args = ap.parse_args()
    print(
        json.dumps(build(args.pbf, args.pack, args.out, args.radius), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
