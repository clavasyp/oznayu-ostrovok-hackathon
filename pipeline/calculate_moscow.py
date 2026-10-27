"""Москва: полный компактный граф, районные очереди отелей и возобновляемые части."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from shapely import STRtree
from shapely.geometry import Point, shape

from calculators.route_geo import OWN, VERSION, RoutePoiCalculator
from calculators.route_graph import PedestrianGraph
from core.packs import PackSource, iter_jsonl, sha256_file, verify
from core.plugins import CityContext, DatasetRef, Subject
from pipeline.build_packs import coverage, new_output, pack_parts, write_pack
from pipeline.build_v9_packs import ROOT, write_jsonl_gz
from pipeline.calculate_city import V9, building_matches, regression


def district_chunks(hotels: list[dict], osm_dir: Path, size: int = 100) -> list[dict]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    features = json.loads((osm_dir / "localities.geojson").read_text())["features"]
    districts = [f for f in features if f["properties"].get("admin_level") == "8"]
    geoms = [shape(f["geometry"]) for f in districts]
    tree = STRtree(geoms)
    grouped = defaultdict(list)
    for h in sorted(hotels, key=lambda r: r["id"]):
        point = Point(h["longitude"], h["latitude"])
        hits = tree.query(point, predicate="intersects")
        # Одинаковое правило при перекрывающихся полигонах: меньший контур, затем имя.
        if len(hits):
            i = min(hits, key=lambda i: (geoms[i].area, districts[i]["properties"].get("name", "")))
            name = districts[i]["properties"].get("name", "unnamed_district")
        else:
            name = "district_not_mapped"
        grouped[name].append(h["id"])
    return [
        {"district": name, "part": offset // size, "hotel_ids": ids[offset : offset + size]}
        for name, ids in sorted(grouped.items())
        for offset in range(0, len(ids), size)
    ]


def run(
    base_packs: Path, osm_dir: Path, out: Path, *, raster_dir: Path, size: int = 100, resume=False
) -> dict:
    start = time.perf_counter()
    inputs = {
        "base_manifest": sha256_file(base_packs / "moscow/manifest.json"),
        "osm_manifest": sha256_file(osm_dir / "manifest.json"),
        "code": {
            p: sha256_file(ROOT / p)
            for p in (
                "calculators/route_graph.py",
                "calculators/route_geo.py",
                "calculators/poi_hours.py",
                "pipeline/calculate_city.py",
                "pipeline/calculate_moscow.py",
            )
        },
        "chunk_size": size,
        "raster_sha256": {p.name: sha256_file(p) for p in sorted(raster_dir.glob("*.tif"))},
    }
    if not inputs["raster_sha256"]:
        raise ValueError("Moscow calculation requires the DSM input directory")
    if resume:
        saved = json.loads((out / "inputs.json").read_text())
        if saved != inputs:
            raise ValueError("Resume inputs/code differ; use a new output directory")
    else:
        out = new_output(out, base_packs, osm_dir, raster_dir)
        out.mkdir(parents=True)
        (out / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    parts = pack_parts(base_packs / "moscow")
    old = [f for f in iter_jsonl(V9 / "integrated/all8_facts.jsonl.gz") if f["city_id"] == "moscow"]
    matches_path = out / "building_match_state.json"
    if matches_path.exists():
        matches, buildings, links, match_report = json.loads(matches_path.read_text())
    else:
        print("Matching complete Moscow addresses and building geometries", flush=True)
        matches, buildings, links, match_report = building_matches(
            parts["hotels"], old, osm_dir, review_nearby=True
        )
        matches_path.write_text(
            json.dumps([matches, buildings, links, match_report], ensure_ascii=False) + "\n"
        )
    chunks = district_chunks(parts["hotels"], osm_dir, size)
    audit = out / "audit"
    audit.mkdir(exist_ok=True)
    hotels = {h["id"]: h for h in parts["hotels"]}
    graph = None
    ref = DatasetRef("osm_city", osm_dir, "OpenStreetMap/Geofabrik", sha256=inputs["osm_manifest"])
    all_rows, chunk_reports = [], []
    for index, chunk in enumerate(chunks):
        folder = audit / f"part_{index:03d}"
        report_file = folder / "complete.json"
        if not report_file.exists():
            if graph is None:
                print(
                    "Building one full compact Moscow graph (district borders do not cut paths)",
                    flush=True,
                )
                manifest = json.loads((osm_dir / "manifest.json").read_text())
                manifest["sha256"] = manifest["source_sha256"]
                graph = PedestrianGraph(
                    "moscow", iter_jsonl(osm_dir / "records.jsonl.gz"), manifest, compact=True
                )
                gc.collect()
                print(f"Graph ready: {len(graph.nids)} nodes, {len(graph.edges)} edges", flush=True)
            folder.mkdir(exist_ok=True)
            subjects = [
                Subject(
                    hid, "hotel", f"hotel:{hid}", hotels[hid]["latitude"], hotels[hid]["longitude"]
                )
                for hid in chunk["hotel_ids"]
            ]
            calc = RoutePoiCalculator()
            facts = calc.run(
                subjects,
                CityContext(
                    "moscow",
                    "Europe/Moscow",
                    osm_dir,
                    {"osm_city": ref},
                    {
                        "building_matches": matches,
                        "shared_graph": graph,
                        "raster_dir": str(raster_dir),
                    },
                ),
            )
            rows = [f.model_dump(mode="json", exclude_none=True) for f in facts]
            write_jsonl_gz(folder / "facts.jsonl.gz", rows)
            for name, output in calc.outputs.items():
                write_jsonl_gz(folder / (name + ".gz"), output)
            files = {p.name: sha256_file(p) for p in folder.glob("*.gz")}
            report_file.write_text(
                json.dumps(
                    chunk | {"facts": len(rows), "diagnostics": calc.diagnostics, "files": files},
                    ensure_ascii=False,
                )
                + "\n"
            )
            print(
                f"Part {index + 1}/{len(chunks)}: {chunk['district']}, "
                f"{len(subjects)} hotels, {len(rows)} facts",
                flush=True,
            )
            del facts, rows, calc
        report = json.loads(report_file.read_text())
        if any(sha256_file(folder / name) != digest for name, digest in report["files"].items()):
            raise ValueError("Damaged saved chunk")
        all_rows.extend(iter_jsonl(folder / "facts.jsonl.gz"))
        chunk_reports.append(report)
    if {f["hotel_id"] for f in all_rows} != set(hotels):
        raise ValueError("Moscow hotel coverage mismatch")
    if not any(f["feature_id"] == "W04" and f["status"] == "estimate" for f in all_rows):
        raise ValueError("No Moscow DSM estimates: check raster coverage before publishing")
    graph_diagnostics = {
        k: chunk_reports[0]["diagnostics"][k]
        for k in ("graph_nodes", "graph_edges", "blocked_pedestrian_nodes")
    }
    del graph
    gc.collect()
    parts["facts"] = [
        f
        for f in parts["facts"]
        if not (f["feature_id"] in OWN and f["status"] != "ostrovok_content")
    ] + all_rows
    parts["buildings"], parts["hotel_buildings"] = buildings, links
    parts["coverage"] = coverage(parts["hotels"], parts["facts"], parts["coverage"])
    original = verify(base_packs / "moscow")
    manifest = write_pack(
        out,
        "moscow",
        parts,
        sources=original.sources
        + [
            PackSource(
                id="geo_moscow",
                provider="OpenStreetMap/Geofabrik",
                release=VERSION,
                sha256=ref.sha256,
                license="ODbL-1.0",
            )
        ],
        built_at=original.built_at,
        code_version="moscow-geo.1",
        notes="Полный каталог и компактный общий граф Москвы; отели посчитаны районными "
        "частями. Входы и доступ не осмотрены, маршруты — estimate.",
    )
    for entry in sorted(base_packs.glob("*/manifest.json")):
        if entry.parent.name != "moscow" and not (out / entry.parent.name).exists():
            shutil.copytree(entry.parent, out / entry.parent.name)
    rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
        1 if sys.platform == "darwin" else 1024
    )
    report = {
        "hotels": len(hotels),
        "geo_facts": len(all_rows),
        "building_matches": match_report,
        "parts": [
            {k: c[k] for k in ("district", "part", "hotel_ids", "facts")} for c in chunk_reports
        ],
        "graph": graph_diagnostics,
        "blocked_node_contacts": 0,
        "feature_statuses": {
            fid: dict(Counter(f["status"] for f in all_rows if f["feature_id"] == fid))
            for fid in OWN
        },
        "v9_comparison_updated_inputs": regression(old, all_rows),
        "comparison_scope": "Updated hotel coordinates, accepted buildings and full scene; "
        "not a fixed-input algorithm control.",
        "peak_rss_mb": round(rss_bytes / 1024**2, 1),
        "seconds": time.perf_counter() - start,
        "pack_version": manifest.pack_version,
        "inputs_digest": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
    }
    (out / "moscow_geo_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-packs", type=Path, required=True)
    p.add_argument("--osm-dir", type=Path, required=True)
    p.add_argument("--raster-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--chunk-size", type=int, default=100)
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    report = run(
        a.base_packs, a.osm_dir, a.out, raster_dir=a.raster_dir, size=a.chunk_size, resume=a.resume
    )
    print(
        json.dumps(
            {k: report[k] for k in ("hotels", "geo_facts", "graph", "peak_rss_mb", "seconds")}
        )
    )


if __name__ == "__main__":
    main()
