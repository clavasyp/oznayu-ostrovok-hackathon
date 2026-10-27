"""Довычислить W04 по сохранённым точным трассам, без повторного построения графа.

Использует тот же dsm_ascent и контракт W04, что полный RoutePoiCalculator. DSM
остаётся оценкой поверхности; реальные высоты тротуара и подходов не предполагаются.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from pyproj import CRS, Transformer

from calculators.route_geo import (
    VERSION,
    attempts,
    dsm_ascent,
    make_geo_fact,
    normalize_route_fact,
    raster_source,
)
from calculators.route_graph import CENTERS
from core.facts import Fact, make_fact_id
from core.packs import PackSource, iter_jsonl, move_provenance_params, sha256_file, verify
from pipeline.build_packs import coverage, new_output, pack_parts, write_pack
from pipeline.calculate_city import V9, regression


def complete(parts: dict, routes: dict, raster_dir: Path, manifest: dict, city: str) -> dict:
    lon, lat = CENTERS[city]
    tr = Transformer.from_crs(
        4326,
        CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"),
        always_xy=True,
    )
    projection = SimpleNamespace(
        tr=tr, inv=Transformer.from_crs(tr.target_crs, 4326, always_xy=True)
    )
    profiles, sources, updated = {}, {}, 0
    for i, raw in enumerate(parts["facts"]):
        if raw["feature_id"] != "W04" or raw.get("algorithm_version") != VERSION:
            continue
        rid = raw.get("params", {}).get("route_id")
        if not rid:
            continue
        if rid not in routes:
            raise ValueError("Missing saved W04 route: " + rid)
        if rid not in profiles:
            profiles[rid] = dsm_ascent({"coordinates": routes[rid]}, projection, raster_dir)
        dsm = profiles[rid]
        if not dsm or not dsm.get("ok"):
            continue
        for tile in dsm["tiles"]:
            sources.setdefault(tile, None)
            if sources[tile] is None:
                sources[tile] = raster_source(tile, raster_dir)
        row = make_geo_fact(
            {"hotel_id": raw["hotel_id"], "city_id": city},
            "W04",
            manifest,
            raw.get("calculated_at"),
            status="estimate",
            value=dsm,
            unit="m",
            params=raw["params"],
            recipe_id="W04-D18-DSM",
            sources=[sources[t] for t in dsm["tiles"]],
            depends_on=[{"kind": "route", "id": rid}]
            + [{"kind": "raster_tile", "id": t} for t in dsm["tiles"]],
            scope_note="Explicit D18 DSM surface estimate, outside mandatory conditions. "
            "Canonical ground-DTM recipe failed; no conversion of DSM to pavement elevations.",
            recipe_attempts=attempts(
                "W04",
                {
                    "W04-R1": "No ground DTM/engineering profile. Copernicus GLO30 is DSM, "
                    "rejected as ground profile.",
                    "W04-R2": "No repeated surveyed or levelled route profile.",
                },
            ),
            catalogue_recipe_not_satisfied=True,
            hard_condition_eligible=False,
        )
        row = move_provenance_params(normalize_route_fact(row))
        row["depends_on"].append({"kind": "route", "id": rid})
        row["fact_id"] = make_fact_id(
            row["hotel_id"],
            row["subject_id"],
            "W04",
            row["params"],
            VERSION + ":" + manifest["source_sha256"],
        )
        parts["facts"][i] = Fact.model_validate(row).model_dump(mode="json", exclude_none=True)
        updated += 1
    return {
        "updated_facts": updated,
        "routes": len(profiles),
        "tiles": {t: s["dataset_sha256"] for t, s in sorted(sources.items())},
        "statuses": dict(Counter(f["status"] for f in parts["facts"] if f["feature_id"] == "W04")),
        "physical_ground_profile_verified": False,
    }


def run(base: Path, audit: Path, osm_dir: Path, raster_dir: Path, out: Path) -> dict:
    started = time.perf_counter()
    out = new_output(out, base, audit, osm_dir, raster_dir)
    parts = pack_parts(base / "moscow")
    needed = {
        f["params"]["route_id"]
        for f in parts["facts"]
        if f["feature_id"] == "W04" and f.get("params", {}).get("route_id")
    }
    routes = {}
    for path in sorted(audit.glob("**/moscow_all_selected_routes.jsonl.gz")):
        for row in iter_jsonl(path):
            rid = row["properties"]["route_id"]
            if rid in needed:
                geom = row["geometry"]
                routes[rid] = (
                    [geom["coordinates"]] if geom["type"] == "Point" else geom["coordinates"]
                )
    manifest = json.loads((osm_dir / "manifest.json").read_text())
    report = complete(parts, routes, raster_dir, manifest, "moscow")
    if not report["updated_facts"]:
        raise ValueError("No route received a DSM estimate; check rasters/coordinates")
    parts["coverage"] = coverage(parts["hotels"], parts["facts"], parts["coverage"])
    original = verify(base / "moscow")
    result = write_pack(
        out,
        "moscow",
        parts,
        sources=original.sources
        + [
            PackSource(
                id="moscow_dsm",
                provider="Copernicus DEM GLO-30",
                license="Copernicus DEM Licence",
                sha256=hashlib.sha256(
                    json.dumps(report["tiles"], sort_keys=True).encode()
                ).hexdigest(),
                tile_sha256=report["tiles"],
            )
        ],
        built_at=original.built_at,
        code_version="moscow-dsm.1",
        notes="Московский граф посчитан районными частями; W04 довычислен по сохранённым "
        "трассам и проверенным DSM. DSM не является DTM; обязательные условия по оценке "
        "не закрываются.",
    )
    for p in base.glob("*/manifest.json"):
        if p.parent.name != "moscow":
            shutil.copytree(p.parent, out / p.parent.name)
    shutil.copy2(base / "building_match_state.json", out / "building_match_state.json")
    previous = json.loads((base / "moscow_geo_report.json").read_text())
    previous["dsm_completion"] = report | {
        "seconds": time.perf_counter() - started,
        "code_sha256": sha256_file(Path(__file__)),
    }
    previous["feature_statuses"]["W04"] = report["statuses"]
    old = [f for f in iter_jsonl(V9 / "integrated/all8_facts.jsonl.gz") if f["city_id"] == "moscow"]
    previous["v9_comparison_updated_inputs"] = regression(old, parts["facts"])
    previous["pack_version"] = result.pack_version
    (out / "moscow_geo_report.json").write_text(
        json.dumps(previous, ensure_ascii=False, indent=2) + "\n"
    )
    (out / "moscow_ascent_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "audit", "osm-dir", "raster-dir", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    print(
        json.dumps(
            run(a.base, a.audit, a.osm_dir, a.raster_dir, a.out), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
