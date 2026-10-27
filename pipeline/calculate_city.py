"""Этап 6: георасчёт полного Сочи, контроль v9, новый пакет и отчёт покрытия."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from shapely import STRtree
from shapely.geometry import Point, shape

from agents.address.match import compare
from agents.address.normalize import osm_addresses, parse_address
from calculators.route_geo import OWN, VERSION, RoutePoiCalculator
from core.packs import PackSource, iter_jsonl, sha256_file, verify
from core.plugins import CityContext, DatasetRef, Subject
from pipeline.build_packs import coverage, new_output, pack_parts, write_pack
from pipeline.build_v9_packs import ROOT, write_jsonl_gz

V9 = ROOT / "research_notes/Расчёт характеристик"


def building_matches(
    hotels: list[dict], old_facts: list[dict], osm_dir: Path, *, review_nearby: bool = False
) -> tuple[list, list, list, dict]:
    """Прежние связи v9 + уникальное точное совпадение адреса и точки внутри полигона.

    Ближайшее здание не принимается. Неоднозначные контуры остаются без новой связи.
    Функция нужна Сочи; московскую очередь людей из этапа 7 не подменяет.
    """
    legacy = defaultdict(set)
    for fact in old_facts:
        if fact["subject_level"] == "building" and fact["status"] in {"observed", "computed"}:
            legacy[fact["hotel_id"]].add(fact["subject_id"])
    needed = {bid for bids in legacy.values() for bid in bids}
    points = [Point(h["longitude"], h["latitude"]) for h in hotels]
    tree = STRtree(points)
    addresses = [parse_address(h.get("address") or "") for h in hotels]
    candidates, records, review = defaultdict(dict), {}, defaultdict(dict)
    for building in iter_jsonl(osm_dir / "buildings.jsonl.gz"):
        bid = building["id"]
        geom = shape(building["geometry"])
        if bid in needed:
            records[bid] = building
        tags = building["tags"]
        if not (tags.get("addr:housenumber") or tags.get("addr2:housenumber")):
            continue
        osm_addr = list(osm_addresses(tags))
        indices = (
            tree.query(geom, predicate="dwithin", distance=0.0027)
            if review_nearby
            else tree.query(geom, predicate="intersects")
        )
        for index in indices:
            hotel = hotels[int(index)]
            if legacy.get(hotel["id"]):
                continue
            address = addresses[int(index)]
            for candidate in osm_addr:
                comparison = compare(address, candidate)
                inside = geom.covers(points[int(index)])
                if (
                    address.street_type
                    and candidate.street_type
                    and address.possession == candidate.possession
                    and comparison == {"street": "same", "house": "exact"}
                    and inside
                ):
                    candidates[hotel["id"]][bid] = {
                        "hotel_address": address.raw,
                        "normalized_hotel": address.text(),
                        "normalized_osm": candidate.text(),
                        "point_covered_by_building": True,
                        "rule": "unique_full_address_and_point_in_polygon",
                    }
                    records[bid] = building
                elif review_nearby and comparison:
                    review[hotel["id"]][bid] = {
                        "normalized_hotel": address.text(),
                        "normalized_osm": candidate.text(),
                        "comparison": comparison,
                        "point_covered_by_building": inside,
                        "reason": "address_or_geometry_requires_human_review",
                        "source_url": f"https://www.openstreetmap.org/{bid}",
                    }
    accepted, links, used = [], [], set()
    for hotel in hotels:
        hid = hotel["id"]
        bids = {b for b in legacy.get(hid, set()) if b in records}
        method = "v9_accepted_link_replay"
        if not bids and len(candidates.get(hid, {})) == 1:
            bids = set(candidates[hid])
            method = "unique_full_address_and_point_in_polygon"
        matches = []
        for bid in sorted(bids):
            building = records[bid]
            source = {
                "provider": "OpenStreetMap",
                "url": f"https://www.openstreetmap.org/{bid}",
                "license": "ODbL-1.0",
                "snapshot_file": "buildings.jsonl.gz",
            }
            matches.append(
                {
                    "building_id": bid,
                    "accepted": True,
                    "geometry_wkb4326": shape(building["geometry"]).wkb_hex,
                    "source": source,
                }
            )
            evidence = {"rule": method}
            if method != "v9_accepted_link_replay":
                evidence |= candidates[hid][bid]
            links.append(
                {"hotel_id": hid, "building_id": bid, "method": "auto", "evidence": evidence}
            )
            used.add(bid)
        if matches:
            accepted.append({"hotel_id": hid, "matches": matches})
    return (
        accepted,
        [records[b] for b in sorted(used)],
        links,
        {
            "matched_hotels": len(accepted),
            "links": len(links),
            "ambiguous_auto_candidates": sum(len(rows) > 1 for rows in candidates.values()),
            "methods": dict(Counter(link["evidence"]["rule"] for link in links)),
            "review_candidates": [
                {
                    "hotel_id": hid,
                    "candidate_building_ids": sorted(rows),
                    "evidence": rows,
                    "state": "proposed",
                    "reason": "multiple_address_footprints",
                }
                for hid, rows in candidates.items()
                if len(rows) > 1
            ]
            + [
                {
                    "hotel_id": hid,
                    "candidate_building_ids": sorted(rows),
                    "evidence": rows,
                    "state": "proposed",
                    "reason": "address_or_geometry_requires_human_review",
                }
                for hid, rows in sorted(review.items())
                if not any(a["hotel_id"] == hid for a in accepted)
            ],
        },
    )


METRICS = {
    "N02": "distance_m",
    "N03": "distance_m",
    "N04": "count",
    "W01": "distance_m",
    "W02": "distance_m",
    "W03": "distance_m",
    "W04": "dsm_positive_dz_estimate_m",
    "W05": "known_step_sections",
    "W06": "known_crossings",
    "Q03": "known_lit_percent",
    "L01": "distance_m",
    "L02": "distance_m",
    "L03": "distance_m",
    "F01": "distance_m",
    "F02": "distance_m",
    "P04": "distance_m",
}


def regression(old: list[dict], new: list[dict]) -> dict:
    def key(fact):
        params = fact.get("params") or {}
        return (
            fact["hotel_id"],
            fact["feature_id"],
            fact["subject_id"] if fact["subject_level"] == "building" else "",
            params.get("arrival_local"),
            params.get("trip_start"),
            params.get("trip_end"),
        )

    baseline = {key(f): f for f in old if f["feature_id"] in OWN}
    current = {key(f): f for f in new if f["feature_id"] in OWN}
    counts, changes = Counter(), []
    for k, a in baseline.items():
        b = current.get(k)
        if b is None and a["status"] == "unknown" and a["feature_id"] in {"N02", "N03"}:
            # Раньше не было корпуса, теперь есть строгая автоматическая связь.
            b = next(
                (
                    row
                    for row in current.values()
                    if row["hotel_id"] == a["hotel_id"]
                    and row["feature_id"] == a["feature_id"]
                    and row["subject_level"] == "building"
                ),
                None,
            )
        if b is None:
            counts["missing_context"] += 1
            changes.append({"key": k, "reason": "context_missing"})
            continue
        metric = METRICS.get(a["feature_id"])
        av, bv = a.get("value") or {}, b.get("value") or {}
        x, y = av.get(metric), bv.get(metric)
        if a["status"] != b["status"]:
            counts["status_changed"] += 1
            changes.append(
                {"key": k, "reason": "status_changed", "old": a["status"], "new": b["status"]}
            )
        elif (x is None) != (y is None):
            counts["metric_missing"] += 1
            changes.append(
                {"key": k, "reason": "metric_missing", "field": metric, "old": x, "new": y}
            )
        elif x is not None and y is not None and not math.isclose(x, y, rel_tol=1e-8, abs_tol=0.01):
            counts["metric_changed"] += 1
            changes.append(
                {
                    "key": k,
                    "reason": "metric_changed",
                    "field": metric,
                    "old": x,
                    "new": y,
                    "delta": y - x,
                    "old_target": av.get("target_id") or av.get("poi_id"),
                    "new_target": bv.get("target_id") or bv.get("poi_id"),
                }
            )
        else:
            counts["equal_status_and_metric"] += 1
    return {
        "baseline_hotels": len({f["hotel_id"] for f in baseline.values()}),
        "contexts": len(baseline),
        "counts": dict(counts),
        "differences": changes,
        "tolerance": {"relative": 1e-8, "absolute": 0.01},
        "scope": "Сверка статуса и основной метрики, не измерение физической точности.",
    }


def explain_changes(
    report: dict, old: list[dict], new: list[dict], cohort: list[dict], hotels: list[dict]
) -> dict:
    """Отделить изменение входных координат/связей от изменения алгоритма."""
    old_hotels = {h["hotel_id"]: h for h in cohort}
    new_hotels = {h["id"]: h for h in hotels}
    old_routes, new_routes = defaultdict(set), defaultdict(set)
    for rows, index in ((old, old_routes), (new, new_routes)):
        for fact in rows:
            value = fact.get("value")
            if isinstance(value, dict) and value.get("route_id"):
                index[(fact["hotel_id"], fact["feature_id"])].add(value["route_id"])
    evidence = {}
    for change in report["differences"]:
        hid, fid = change["key"][:2]
        a, b = old_hotels[hid], new_hotels[hid]
        moved = (a["latitude"], a["longitude"]) != (b["latitude"], b["longitude"])
        if (
            change["reason"] == "metric_changed"
            and moved
            and old_routes[(hid, fid)] == new_routes[(hid, fid)]
        ):
            reason = "snapshot_coordinates_changed_same_route"
        elif (
            change["reason"] == "status_changed"
            and fid in {"N02", "N03"}
            and change["old"] == "unknown"
            and change["new"] == "computed"
        ):
            reason = "new_unique_full_address_and_point_building_match"
        else:
            reason = "requires_input_review"
        change["explanation"] = reason
        evidence[hid] = {
            "old_coordinates": [a["longitude"], a["latitude"]],
            "new_coordinates": [b["longitude"], b["latitude"]],
            "snapshot_source": b.get("source", {}).get("url"),
        }
    return evidence


def control_v9(
    old_facts: list[dict], matches: list[dict], ref: DatasetRef, raster_dir: Path
) -> dict:
    osm_dir = ref.path
    params = {"raster_dir": str(raster_dir)}
    # Сначала контроль на исходных 89 координатах и сцене v9; полный срез не урезается.
    cohort = [h for h in iter_jsonl(V9 / "cohort/hotels.jsonl") if h["city_id"] == "sochi"]
    subjects = [
        Subject(
            hotel_id=h["hotel_id"],
            subject_level="hotel",
            subject_id=f"hotel:{h['hotel_id']}",
            latitude=h["latitude"],
            longitude=h["longitude"],
        )
        for h in cohort
    ]
    calculator = RoutePoiCalculator()
    legacy_pairs = {
        (f["hotel_id"], f["subject_id"])
        for f in old_facts
        if f["subject_level"] == "building" and f["status"] in {"computed", "observed"}
    }
    control_matches = [
        row
        | {
            "matches": [
                b for b in row["matches"] if (row["hotel_id"], b["building_id"]) in legacy_pairs
            ]
        }
        for row in matches
    ]
    controls = calculator.run(
        subjects,
        CityContext(
            city_id="sochi",
            timezone="Europe/Moscow",
            data_dir=osm_dir,
            datasets={"osm_city": ref},
            params=params | {"compat_v9": True, "building_matches": control_matches},
        ),
    )
    control_report = regression(old_facts, [f.model_dump(mode="json") for f in controls])
    control_report["inputs"] = {
        "cohort_sha256": sha256_file(V9 / "cohort/hotels.jsonl"),
        "facts_sha256": sha256_file(V9 / "integrated/sochi_facts.jsonl"),
        "osm_manifest_sha256": sha256_file(osm_dir / "manifest.json"),
        "building_link_policy": "v9_accepted_links_only",
        "scene_policy": "v9_first_vertex_bbox_for_control_only",
        "raster_sha256": {p.name: sha256_file(p) for p in sorted(raster_dir.glob("*.tif"))},
    }
    return control_report


def run_city(base_packs: Path, osm_dir: Path, raster_dir: Path, out: Path) -> dict:
    out = new_output(out, base_packs, osm_dir, raster_dir)
    parts = pack_parts(base_packs / "sochi")
    old_facts = list(iter_jsonl(V9 / "integrated/sochi_facts.jsonl"))
    matches, buildings, links, match_report = building_matches(parts["hotels"], old_facts, osm_dir)
    ref = DatasetRef(
        id="osm_city",
        path=osm_dir,
        provider="OpenStreetMap/Geofabrik",
        sha256=sha256_file(osm_dir / "manifest.json"),
        license="ODbL-1.0",
    )
    params = {"building_matches": matches, "raster_dir": str(raster_dir)}
    control_report = control_v9(old_facts, matches, ref, raster_dir)
    if control_report["differences"]:
        raise ValueError("Контроль v9 дал расхождения; полный расчёт остановлен до проверки")
    print("Контроль v9: " + json.dumps(control_report["counts"]), flush=True)
    cohort = [h for h in iter_jsonl(V9 / "cohort/hotels.jsonl") if h["city_id"] == "sochi"]
    calculator = RoutePoiCalculator()
    full_subjects = [
        Subject(
            hotel_id=h["id"],
            subject_level="hotel",
            subject_id=f"hotel:{h['id']}",
            latitude=h["latitude"],
            longitude=h["longitude"],
        )
        for h in parts["hotels"]
    ]
    facts = calculator.run(
        full_subjects,
        CityContext(
            city_id="sochi",
            timezone="Europe/Moscow",
            data_dir=osm_dir,
            datasets={"osm_city": ref},
            params=params,
        ),
    )
    rows = [f.model_dump(mode="json", exclude_none=True) for f in facts]
    if {f["hotel_id"] for f in rows} != {h["id"] for h in parts["hotels"]}:
        raise ValueError("Не все отели обработаны геокалькулятором")
    common_ids = {h["hotel_id"] for h in cohort}
    common_report = regression(old_facts, [f for f in rows if f["hotel_id"] in common_ids])
    common_evidence = explain_changes(common_report, old_facts, rows, cohort, parts["hotels"])
    parts["facts"] = [
        f
        for f in parts["facts"]
        if not (f["feature_id"] in OWN and f["status"] != "ostrovok_content")
    ] + rows
    parts["buildings"], parts["hotel_buildings"] = buildings, links
    parts["coverage"] = coverage(parts["hotels"], parts["facts"], parts["coverage"])
    original = verify(base_packs / "sochi")
    manifest = write_pack(
        out,
        "sochi",
        parts,
        sources=original.sources
        + [
            PackSource(
                id="geo_sochi",
                provider="OpenStreetMap/Geofabrik",
                release=VERSION,
                sha256=ref.sha256,
                license="ODbL-1.0",
            )
        ],
        built_at=original.built_at,
        code_version=VERSION,
        notes="Полный каталог Сочи + георасчёт. Непроверенные входы/подходы и DSM остаются "
        "оценками; unknown не означает отсутствие сервиса. Прочие 7 городов — из входного пакета.",
    )
    for entry in sorted(base_packs.glob("*/manifest.json")):
        if entry.parent.name != "sochi":
            shutil.copytree(entry.parent, out / entry.parent.name)
    audit = out / "audit"
    audit.mkdir()
    for name, output in calculator.outputs.items():
        write_jsonl_gz(audit / f"{name}.gz", output)
    report = {
        "algorithm_version": VERSION,
        "hotels": len(parts["hotels"]),
        "geo_facts": len(rows),
        "feature_ids": OWN,
        "feature_statuses": {
            fid: dict(Counter(f["status"] for f in rows if f["feature_id"] == fid)) for fid in OWN
        },
        "building_matches": match_report,
        "control_v9": control_report,
        "full_scene_common_hotels": common_report,
        "common_input_evidence": common_evidence,
        "diagnostics": calculator.diagnostics,
        "pack_version": manifest.pack_version,
        "full_scene_difference_inputs": [
            "Полные геометрии на расширенном срезе вместо фильтра по первой вершине bbox v9.",
            (
                "Координаты снимка 24.10 для совпавших masterId; контроль v9 использует "
                "исходную когорту и только прежние связи с корпусами."
            ),
        ],
    }
    (out / "sochi_geo_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-packs", required=True, type=Path)
    parser.add_argument("--osm-dir", required=True, type=Path)
    parser.add_argument("--raster-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--control-only", action="store_true", help="только повтор на 89 отелях v9")
    args = parser.parse_args()
    if args.control_only:
        out = new_output(args.out, args.base_packs, args.osm_dir, args.raster_dir)
        parts = pack_parts(args.base_packs / "sochi")
        old = list(iter_jsonl(V9 / "integrated/sochi_facts.jsonl"))
        matches, _, _, _ = building_matches(parts["hotels"], old, args.osm_dir)
        report = control_v9(
            old, matches, DatasetRef("osm_city", args.osm_dir, "OpenStreetMap"), args.raster_dir
        )
        out.mkdir(parents=True, exist_ok=True)
        (out / "control_v9.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        print(json.dumps(report["counts"], ensure_ascii=False))
        return
    report = run_city(args.base_packs, args.osm_dir, args.raster_dir, args.out)
    print(
        json.dumps(
            {k: report[k] for k in ("hotels", "geo_facts", "feature_statuses", "building_matches")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
