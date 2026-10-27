"""Фасады, D34 и паспорта итогового пакета; построение только вне репозитория."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from calculators.route_facades import enrich
from connectors.twogis import grid_plan
from core.packs import PackSource, sha256_file, verify
from pipeline.build_packs import new_output, pack_parts, write_pack
from pipeline.building_review import duplicate_listings, import_v11, items
from pipeline.nearby_tables import build
from pipeline.twogis_layers import merge

# .2: без пар «отель → место», места только с категорией (D41); .3: вероятные дубли карточек
# Островка — кандидаты D33 `duplicate_listing`
VERSION = "seva-full.3"
NOTES = (
    "Полный каталог, георасчёты, корпуса/геометрические фасады и OSM D34. "
    "Расстояния до мест и станций — по прямой, доступ не подтверждён. "
    "Места рядом считает запрос PostGIS; в пакете только места с категорией словаря мест "
    "или характеристики каталога. "
)


def run(
    base_packs: Path,
    osm_root: Path,
    out: Path,
    city: str,
    *,
    v11_candidates: Path | None = None,
    v11_sha256: str | None = None,
    built_at: datetime | None = None,
    twogis_bundle: Path | None = None,
) -> dict:
    started = time.perf_counter()
    built_at = built_at or datetime.now(UTC)
    if built_at.tzinfo is None:
        raise ValueError("Build timestamp requires timezone")
    out = new_output(out, base_packs, osm_root)
    source_pack = base_packs / city
    parts = pack_parts(source_pack)
    original = verify(source_pack)
    source_file = osm_root / city / "manifest.json"
    osm = json.loads(source_file.read_text())
    facades = enrich(
        parts,
        {
            "dataset_version": osm["source_date"],
            "dataset_sha256": osm["source_sha256"],
            "snapshot_file": "buildings.jsonl.gz",
        },
    )
    print(f"{city}: facades ready, building D34 tables", flush=True)
    nearby = build(parts, osm_root / city, city)
    print(f"{city}: {nearby['places']} places, pairs are computed on request", flush=True)
    state_file = base_packs / "building_match_state.json"
    sochi_report = base_packs / "sochi_geo_report.json"
    if city == "moscow" and state_file.is_file():
        match_report = json.loads(state_file.read_text())[-1]
    elif city == "sochi" and sochi_report.is_file():
        match_report = json.loads(sochi_report.read_text())["building_matches"]
    else:
        match_report = {"review_candidates": []}
    parts["curation_items"] = items(
        parts["hotels"], parts.get("hotel_buildings", []), match_report, sha256_file(source_file)
    )
    parts["curation_items"] += duplicate_listings(parts["hotels"], parts.get("room_types", []))
    if v11_candidates is not None:
        if not v11_sha256:
            raise ValueError("v11 SHA256 is required")
        parts["curation_items"] += import_v11(
            v11_candidates, v11_sha256, {h["id"] for h in parts["hotels"]}, city
        )
    twogis_sources = [merge(parts, twogis_bundle, city)] if twogis_bundle is not None else []
    manifest = write_pack(
        out,
        city,
        parts,
        sources=[s for s in original.sources if s.id not in {"2gis", "d34_d39_osm"}]
        + twogis_sources
        + [
            PackSource(
                id="d34_d39_osm",
                provider="OpenStreetMap/Geofabrik",
                release=osm["source_date"],
                sha256=sha256_file(source_file),
                license="ODbL-1.0",
                commercial_ok=True,
            )
        ],
        built_at=built_at,
        code_version=VERSION,
        notes=NOTES
        + (
            "2ГИС добавлен отдельными файлами с commercial_ok=false. "
            if twogis_bundle
            else "2ГИС bundle не передан. "
        )
        + "Пустая transit_times не означает отсутствие транспорта; "
        "вне датированных ответов время неизвестно.",
    )
    grid = grid_plan(parts["hotels"])
    counts = Counter()
    for entry in manifest.files:
        counts[entry.kind] += entry.rows
    report = {
        "city": city,
        "hotels": len(parts["hotels"]),
        "facades": facades,
        "nearby": nearby,
        "twogis": {
            "live_test": False,
            "reason": "bundle_loaded_without_network" if twogis_bundle else "bundle_not_supplied",
            "bundle_loaded": bool(twogis_bundle),
            "grid_cells": len(grid),
            "planned_minimum_places_units_per_category": len(grid),
            "demo_scope": "connector checks only; bulk needs subscription",
            "requests_made": 0,
        },
        "seconds": time.perf_counter() - started,
        "pack_version": manifest.pack_version,
        "bytes": sum(f.bytes for f in manifest.files),
        "files": dict(counts),
    }
    (out / (city + "_final_report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-packs", required=True, type=Path)
    p.add_argument("--osm-root", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--city", required=True, choices=("moscow", "sochi"))
    p.add_argument("--v11-candidates", type=Path)
    p.add_argument("--v11-sha256")
    p.add_argument("--twogis-bundle", type=Path)
    p.add_argument(
        "--built-at",
        type=datetime.fromisoformat,
        help="Explicit build time for reproducible manifests",
    )
    a = p.parse_args()
    print(
        json.dumps(
            run(
                a.base_packs,
                a.osm_root,
                a.out,
                a.city,
                v11_candidates=a.v11_candidates,
                v11_sha256=a.v11_sha256,
                built_at=a.built_at,
                twogis_bundle=a.twogis_bundle,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
