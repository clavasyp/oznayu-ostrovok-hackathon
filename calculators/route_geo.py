"""Геокалькуляторы Севы: перенесённые расчёты v9 с контекстом города и Fact.

Граф, маршруты, ограничения и метрики сохранены из scripts/geo.py. Пути старых
кешей заменены явными входами. Сеть и записи в research_notes отсутствуют.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import time
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

import numpy as np
from scipy.sparse.csgraph import connected_components
from shapely import STRtree, wkb
from shapely.geometry import LineString, Point, mapping
from shapely.ops import transform as gtransform

from calculators.poi_hours import opening_state as evaluate_hours
from calculators.route_graph import (
    BUSY,
    CENTERS,
    COAST,
    COUNTRY,
    MARINE_ADJACENCY_TOLERANCE_M,
    PedestrianGraph,
    crossing_metrics,
    lighting_metrics,
    osm_source,
    steps_metrics,
)
from core.facts import Fact, make_fact_id
from core.packs import iter_jsonl, move_provenance_params, sha256_file
from core.plugins import CityContext, Subject, register_calculator

VERSION = "route-poi.1"
BASE = Path(__file__).resolve().parents[1] / "research_notes" / "Расчёт характеристик"
TRIPS = [
    {"start": "2026-11-14", "end": "2026-11-17", "arrival_local": "2026-11-14T02:00:00"},
    {"start": "2027-07-15", "end": "2027-07-18", "arrival_local": "2027-07-17T02:00:00"},
]
TZ = {"moscow": "Europe/Moscow", "sochi": "Europe/Moscow"}
OWN = "N02 N03 N04 W01 W02 W03 W04 W05 W06 L01 L02 L03 F01 F02 P03 P04 Q03".split()


def opening_state(poi, date, city, kitchen=False):
    return evaluate_hours(
        poi["tags"], date, timezone=TZ[city], country=COUNTRY[city], kitchen=kitchen
    )


def attempts(feature_id, reasons=None, success_recipe=None):
    reasons = reasons or {}
    result = [
        {"recipe_id": key, "state": "rejected", "reason": reason, "data_kind": "Б"}
        for key, reason in reasons.items()
    ]
    if success_recipe:
        result.append({"recipe_id": success_recipe, "state": "accepted", "data_kind": "Б"})
    return result


def make_geo_fact(hotel, feature_id, manifest, stamp, **kwargs):
    value = kwargs.pop("value", None)
    status = kwargs.pop("status", "unknown")
    subject = kwargs.pop("subject_id", None) or f"hotel:{hotel['hotel_id']}"
    params = kwargs.pop("params", None) or {}
    sources = kwargs.pop("sources", None) or [
        {
            "provider": "OpenStreetMap",
            "dataset_version": manifest.get("source_date"),
            "dataset_sha256": manifest.get("source_sha256"),
            "license": "ODbL-1.0",
            "url": "https://www.openstreetmap.org/copyright",
        }
    ]
    note = kwargs.get("scope_note") or kwargs.get("unknown_reason")
    confidence = kwargs.pop("confidence", None) or {
        "level": "unknown" if status == "unknown" else "low" if status == "estimate" else "medium",
        "reason": note,
    }
    return Fact(
        fact_id=make_fact_id(hotel["hotel_id"], subject, feature_id, params, VERSION),
        hotel_id=hotel["hotel_id"],
        city_id=hotel["city_id"],
        subject_id=subject,
        subject_level=kwargs.pop("subject_level", "hotel"),
        feature_id=feature_id,
        params=params,
        value=value,
        status=status,
        sources=sources,
        algorithm_version=VERSION,
        calculated_at=stamp,
        confidence=confidence,
        **kwargs,
    ).model_dump(mode="json")


def raster_source(tile, raster_dir):
    return {
        "provider": "Copernicus DEM GLO-30",
        "dataset_sha256": sha256_file(raster_dir / tile),
        "url": "https://copernicus-dem-30m.s3.amazonaws.com/"
        + tile.removesuffix(".tif")
        + "/"
        + tile,
        "snapshot_file": "raster_tiles/" + tile,
        "license": "Copernicus DEM Licence",
        "license_url": "https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM",
        "surface_model": "DSM, not DTM",
        "physical_observation_at": None,
    }


def normalize_route_fact(row):
    """Use a route subject and reject undefined percentages on a zero graph."""
    r = {**row}
    if (
        r["feature_id"] == "Q03"
        and r["status"] == "computed"
        and not r["value"].get("total_graph_length_m", 0)
    ):
        reason = "undefined_lighting_fraction_zero_graph_length"
        r.update(
            {
                "status": "unknown",
                "value": None,
                "unknown_reason": reason,
                "recipe_id": None,
                "confidence": {
                    "level": "unknown",
                    "reason": (
                        "The mapped graph route has zero length, so its lighting fraction "
                        "has no denominator. Unverified connector gaps are not assumed "
                        "lit/unlit."
                    ),
                },
                "component_diagnostics": {
                    **r.get("component_diagnostics", {}),
                    "zero_graph_lighting_metrics": row["value"],
                },
                "recipe_attempts": attempts(
                    "Q03",
                    {
                        "Q03-R1": "No full surveyed route lighting inventory.",
                        "Q03-R2": (
                            "Zero mapped graph length: lighting percentages are undefined, not"
                            " zero percent."
                        ),
                    },
                ),
            }
        )
    rid = r["params"].get("route_id")
    if r["feature_id"] in {"W04", "W05", "W06", "Q03"} and rid:
        r.update({"subject_level": "route", "subject_id": "route:" + rid})
    identity = json.dumps(
        [
            r["city_id"],
            r["hotel_id"],
            r["feature_id"],
            r["subject_level"],
            r["subject_id"],
            r["params"],
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    r["fact_id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return r


def dsm_ascent(route, graph, raster_dir):
    try:
        import rasterio
        from scipy.ndimage import median_filter

        if len(route["coordinates"]) < 2:
            return None
        line = gtransform(graph.tr.transform, LineString(route["coordinates"]))
        positions = np.linspace(0, line.length, max(2, int(math.ceil(line.length / 30)) + 1))
        points = [line.interpolate(float(d)) for d in positions]
        ll = [graph.inv.transform(p.x, p.y) for p in points]
        values = [None] * len(ll)
        used = []
        for fp in raster_dir.glob("Copernicus_DSM*.tif"):
            with rasterio.open(fp) as src:
                inds = [
                    i
                    for i, (x, y) in enumerate(ll)
                    if src.bounds.left < x < src.bounds.right
                    and src.bounds.bottom < y < src.bounds.top
                ]
                if not inds:
                    continue
                used.append(fp.name)
                for i, v in zip(inds, src.sample([ll[i] for i in inds], masked=True), strict=True):
                    if not np.ma.is_masked(v[0]):
                        values[i] = float(v[0])
        if any(v is None for v in values):
            return {
                "ok": False,
                "reason": "DSM_tile_or_nodata_gap",
                "valid_samples": sum(v is not None for v in values),
                "samples": len(values),
            }
        a = np.asarray(values)
        sm = median_filter(a, size=3, mode="nearest")
        delta = np.diff(sm)
        total = float(np.maximum(delta, 0).sum())
        return {
            "ok": True,
            "dsm_positive_dz_estimate_m": total,
            "sample_step_m": 30,
            "samples": len(values),
            "filter": "3-sample running median; endpoint nearest",
            "tiles": used,
            "ground_ascent_not_measured": True,
            "model": "CopernicusGLO30 DSM; building/vegetation surface, not DTM",
            "error_interval_m": None,
            "error_reason": "No independent ground profile; no fabricated error interval.",
        }
    except Exception as e:
        return {"ok": False, "reason": "DSM_sampling_failed:" + str(e)[:120]}


def _calculate(city, hs, graph, building_matches, raster_dir, stamp, trips):
    outputs = {}

    def write_jsonl(path, rows):
        outputs[path.name] = list(rows)

    def dsm_source(tile, raster_dir):
        return raster_source(tile, raster_dir)

    def fact(hotel, feature_id, **kwargs):
        return make_geo_fact(hotel, feature_id, graph.manifest, stamp, **kwargs)

    start = time.perf_counter()
    outdir = BASE / "results/geo"
    load_seconds = time.perf_counter() - start
    facts = []
    routes = []
    poi_audit = []
    targets_out = []
    timings = collections.defaultdict(float)
    diag = {
        "city_id": city,
        "hotels": len(hs),
        "graph_nodes": len(graph.nids),
        "graph_edges": len(graph.edges),
        "load_seconds": load_seconds,
        "input_manifest": graph.manifest,
        "no_new_http_requests": True,
        "blocked_pedestrian_nodes": len(graph.blocked_nodes),
        "blocked_node_policy": (
            "explicit foot/access restrictions and unresolved conditional "
            "nodes; locked gates without explicit foot permission. Bollard "
            "alone is not blocked."
        ),
        "gtfs_calendar_checks": [],
    }
    exclusions_file = BASE / "qa/geo/source_quality_exclusions.json"
    quality_exclusions = json.loads(exclusions_file.read_text()) if exclusions_file.exists() else []
    joins = collections.defaultdict(list)
    for row in building_matches:
        joins[str(row["hotel_id"])].append(row)
    joinfile = Path("hotel_buildings.jsonl.gz")
    beach = graph.beach_targets() if city in COAST else []
    diag["marine_beach_gate"] = {
        "method_version": "marine-adjacency-v1",
        "tolerance_m": MARINE_ADJACENCY_TOLERANCE_M,
        "coastline_segments": len(graph.coastlines),
        "mapped_beaches": len(graph.beaches),
        "accepted_geometry_count": sum(r["accepted"] for r in graph.beach_classifications),
        "eligible_graph_contact_candidates": len(beach),
        "rejected_meaning": "marine_adjacency_unconfirmed, not proof that the beach is river/lake",
    }
    reference_center = Point(*graph.tr.transform(*CENTERS[city]))
    _, component_labels = connected_components(graph.A, directed=False)
    sizes = np.bincount(component_labels)
    largest_component = int(np.argmax(sizes))
    center_pool = np.flatnonzero(component_labels == largest_component)
    offsets = np.linalg.norm(
        graph.xy[center_pool] - np.asarray([reference_center.x, reference_center.y]), axis=1
    )
    center_index = int(center_pool[int(np.argmin(offsets))])
    center_gap = float(np.min(offsets))
    center = {
        "sid": f"project:center:{city}:osm-node-{graph.nids[center_index]}",
        "point": Point(graph.xy[center_index]),
        "tags": {
            "name": "Explicit mapped pedestrian reference node nearest project city-center point "
            + city
        },
        "verified_entrance": False,
    }
    diag["center_target"] = {
        "osm_node_id": graph.nids[center_index],
        "coordinates": graph.inv.transform(*graph.xy[center_index]),
        "selection_reference_coordinates": CENTERS[city],
        "reference_to_node_m": float(center_gap),
        "selection_rule": (
            "nearest node to reference center in largest connected mapped "
            "pedestrian network component"
        ),
        "component_nodes": int(sizes[largest_component]),
        "meaning": (
            "An explicit chosen point near the center, not an implicit "
            "administrative center or hotel entrance."
        ),
    }
    bars = [
        r
        for r in graph.pois
        if r["tags"].get("amenity") in {"bar", "nightclub", "music_venue"}
        or r["tags"].get("club") == "music"
    ]
    bar_tree = STRtree([r["geometry"] for r in bars]) if bars else None
    for h in hs:
        p = graph.project_hotel(h)
        covered = graph.covered(h)
        t0 = time.perf_counter()
        # N04 counts mapped objects in this snapshot, not all real nightlife venues.
        nearby = (
            [bars[int(i)] for i in sorted(bar_tree.query(p, predicate="dwithin", distance=300))]
            if bar_tree is not None
            else []
        )
        local_pois = (
            [
                graph.pois[int(i)]
                for i in sorted(graph.poi_tree.query(p, predicate="dwithin", distance=30000))
            ]
            if graph.poi_tree is not None
            else []
        )
        seen = set()
        dedup = []
        for r in nearby:
            name = (r["tags"].get("name") or "").strip().casefold()
            dup = any(
                name
                and name == (q["tags"].get("name") or "").strip().casefold()
                and r["point"].distance(q["point"]) < 15
                for q in dedup
            )
            if r["sid"] in seen or dup:
                continue
            seen.add(r["sid"])
            dedup.append(r)
        if covered:
            f = fact(
                h,
                "N04",
                value={
                    "count": len(dedup),
                    "objects": [
                        {
                            "id": r["sid"],
                            "name": r["tags"].get("name"),
                            "type": r["tags"].get("amenity"),
                            "distance_m": p.distance(r["geometry"]),
                            "opening_hours": r["tags"].get("opening_hours"),
                        }
                        for r in dedup
                    ],
                    "completeness": (
                        "Objects mapped in this OSM extraction; not all real venues. Zero "
                        "means none recorded."
                    ),
                    "unknown_hours_count": sum(not r["tags"].get("opening_hours") for r in dedup),
                },
                unit="count",
                status="computed",
                recipe_id="N04-R2",
                params={
                    "radius_m": 300,
                    "types": ["bar", "nightclub", "music_venue"],
                    "distance_origin": "OTAhotelpoint",
                    "buffer_operator": "minimum geometry distance",
                },
                sources=[osm_source(r, graph.manifest) for r in dedup]
                or [
                    {
                        "provider": "OpenStreetMap",
                        "dataset_version": graph.manifest.get("source_date"),
                        "dataset_sha256": graph.manifest.get("sha256"),
                        "license": "ODbL1.0",
                        "url": "https://www.openstreetmap.org/copyright",
                    }
                ],
                scope_note="Source-scoped mapped-count, not acoustic level or indoor quiet.",
                recipe_attempts=attempts(
                    "N04",
                    {"N04-R1": "No accepted official per-object nightlife inventory in cache."},
                    "N04-R2",
                ),
            )
        else:
            f = fact(h, "N04", unknown_reason="hotel_outside_cached_OSM_extent")
        facts.append(f)
        for fid, key, records, tree in [
            ("N02", "road_classes", graph.busy, graph.busy_tree),
            ("N03", "rail_classes", graph.rail, graph.rail_tree),
        ]:
            jj = joins.get(h["hotel_id"], [])
            accepted = []
            for j in jj:
                # Common building-match protocol accepts flat geometry or candidate list.
                candidates = j.get("matches", j.get("accepted_matches", [j]))
                for b in candidates:
                    if b.get("accepted") is False or b.get("match_status") in {
                        "rejected",
                        "ambiguous",
                        "unmatched",
                    }:
                        continue
                    gw = (
                        b.get("geometry_wkb4326")
                        or b.get("geometry_wkb")
                        or b.get("building_geometry_wkb")
                    )
                    if gw:
                        accepted.append(b)
            if accepted and tree:
                for b in accepted:
                    bg = gtransform(
                        graph.tr.transform,
                        wkb.loads(
                            b.get("geometry_wkb4326")
                            or b.get("geometry_wkb")
                            or b.get("building_geometry_wkb"),
                            hex=True,
                        ),
                    )
                    i = int(tree.nearest(bg))
                    r = records[i]
                    dist = bg.distance(r["geometry"])
                    sid = b.get(
                        "building_id", b.get("source_id", b.get("osm_id", "unknown_building"))
                    )
                    facts.append(
                        fact(
                            h,
                            fid,
                            value={
                                "distance_m": dist,
                                "target_id": r["sid"],
                                "target_class": r["tags"].get(
                                    "highway" if fid == "N02" else "railway"
                                ),
                                "target_name": r["tags"].get("name"),
                                "building_id": sid,
                            },
                            unit="m",
                            status="computed",
                            subject_level="building",
                            subject_id=str(sid),
                            recipe_id=fid + "-R2",
                            params={
                                key: sorted(BUSY)
                                if fid == "N02"
                                else ["rail", "light_rail", "tram"],
                                "distance_origin": "accepted building footprint",
                                "target_geometry": "axis",
                                "rail_status_convention": (
                                    "rail/light_rail/tram mapped active; "
                                    "disused/abandoned/construction/proposed excluded"
                                )
                                if fid == "N03"
                                else None,
                            },
                            sources=[
                                osm_source(r, graph.manifest),
                                b.get(
                                    "source",
                                    {"building_join": str(joinfile), "evidence": b.get("evidence")},
                                ),
                            ],
                            scope_note=(
                                "One matched building; not all hotel buildings or window distance."
                                " N03 operational status is OSM mapped state, not inspected train "
                                "frequency."
                            ),
                            recipe_attempts=attempts(
                                fid,
                                {
                                    fid + "-R1": (
                                        "No accepted official road/active-rail layer for this "
                                        "building in "
                                        "cache."
                                    )
                                },
                                fid + "-R2",
                            ),
                        )
                    )
            elif covered and tree:
                i = int(tree.nearest(p))
                r = records[i]
                facts.append(
                    fact(
                        h,
                        fid,
                        unknown_reason="verified_hotel_building_footprint_missing_or_ambiguous",
                        params={
                            key: sorted(BUSY) if fid == "N02" else ["rail", "light_rail", "tram"]
                        },
                        component_diagnostics={
                            "point_to_axis_distance_m": p.distance(r["geometry"]),
                            "target_id": r["sid"],
                            "not_feature_value": "Point distance is not footprint distance.",
                        },
                    )
                )
            else:
                facts.append(
                    fact(
                        h,
                        fid,
                        unknown_reason="source_extent_or_matching_source_geometry_unavailable",
                    )
                )
        facts.append(
            fact(
                h,
                "N01",
                unknown_reason=(
                    "No licensed georeferenced accepted facade-height Lnight layer, "
                    "complete emissions model, or independent acoustic validation in "
                    "cached inputs; road distance is not dB."
                ),
            )
        )
        timings["N"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        route_candidates = {}
        if city in COAST:
            route_candidates["beach"] = (
                graph.choose(h, beach, "beach")
                if covered
                else {"ok": False, "reason": "hotel_outside_cached_OSM_extent"}
            )
        route_candidates["stop"] = (
            graph.choose(h, graph.stops, "stop")
            if covered
            else {"ok": False, "reason": "hotel_outside_cached_OSM_extent"}
        )
        route_candidates["center"] = (
            graph.route(h, center, "center")
            if covered
            else {"ok": False, "reason": "hotel_outside_cached_OSM_extent"}
        )
        for fid, kind in [("W01", "beach"), ("W02", "stop"), ("W03", "center")]:
            if kind == "beach" and city not in COAST:
                facts.append(
                    fact(
                        h,
                        fid,
                        status="not_applicable",
                        params={"target": "marine_beach"},
                        scope_note=(
                            "Moscow/Kazan/Minsk inland; this parameter is marine beach, not "
                            "river/lake recreation."
                        ),
                    )
                )
                continue
            r = route_candidates[kind]
            if not r["ok"]:
                facts.append(
                    fact(
                        h,
                        fid,
                        unknown_reason=r["reason"],
                        params={"target_kind": kind},
                        component_diagnostics=r,
                    )
                )
                continue
            facts.append(
                fact(
                    h,
                    fid,
                    status="estimate",
                    value={
                        "distance_m": r["length_estimate_m"],
                        "graph_length_m": r["graph_length_m"],
                        "hotel_connector_gap_m": r["hotel_snap_m"],
                        "target_connector_gap_m": r["target_snap_m"],
                        "route_id": r["route_id"],
                        "target_id": r["target_id"],
                        "target_name": r["target_name"],
                        "marine_classification": r["target"].get("marine_classification")
                        if kind == "beach"
                        else None,
                        "verified_hotel_entrance": False,
                        "verified_target_entrance": r["target_verified_entrance"],
                    },
                    unit="m",
                    recipe_id=fid + "-R2",
                    params={
                        "target_kind": kind,
                        "target_id": r["target_id"],
                        "walking_graph": (
                            "OSM shared nodes; explicit restricted/conditional nodes excluded;"
                            " no joins at mere geometry crossing"
                        ),
                        "private_or_conditional_access": "excluded",
                        "maximum_unverified_snap_m": 100,
                        "marine_adjacency_tolerance_m": MARINE_ADJACENCY_TOLERANCE_M
                        if kind == "beach"
                        else None,
                    },
                    scope_note=(
                        "Research route estimate; actual legal hotel entrance and "
                        "destination entrance need verification. Beach contact node is not"
                        " a verified public beach gate. Default road foot-access rules "
                        "need local QA. D18: excluded from mandatory constraints."
                    ),
                    sources=[
                        {
                            "provider": "OpenStreetMap",
                            "dataset_version": graph.manifest.get("source_date"),
                            "dataset_sha256": graph.manifest.get("sha256"),
                            "license": "ODbL1.0",
                            "url": "https://www.openstreetmap.org/copyright",
                        }
                    ],
                    recipe_attempts=attempts(
                        fid,
                        {
                            fid + "-R1": (
                                "No official surveyed continuous pedestrian graph/verified "
                                "entrance pair."
                            )
                        },
                        fid + "-R2",
                    ),
                    hard_condition_eligible=False,
                )
            )
            routes.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": r["coordinates"]}
                    if len(r["coordinates"]) > 1
                    else mapping(Point(r["coordinates"][0])),
                    "properties": {
                        "hotel_id": h["hotel_id"],
                        "city_id": city,
                        "route_id": r["route_id"],
                        "kind": kind,
                        "target_id": r["target_id"],
                        "graph_length_m": r["graph_length_m"],
                        "hotel_snap_m": r["hotel_snap_m"],
                        "target_snap_m": r["target_snap_m"],
                        "node_ids": r["node_ids"],
                        "edge_way_ids": [e["sid"] for e in r["edges"]],
                        "validity": "estimate, endpoints and default access not inspected",
                    },
                }
            )
        main = (
            route_candidates.get("beach")
            if route_candidates.get("beach", {}).get("ok")
            else route_candidates["stop"]
            if route_candidates["stop"].get("ok")
            else route_candidates["center"]
        )
        if main.get("ok"):
            dsm = dsm_ascent(main, graph, raster_dir)
            sp = steps_metrics(main["edges"])
            li = lighting_metrics(main["edges"])
            cr = crossing_metrics(main, graph)
            params = {
                "route_id": main["route_id"],
                "target_kind": main["target_kind"],
                "target_id": main["target_id"],
            }
            for fid, value in [("W05", sp), ("W06", cr), ("Q03", li)]:
                facts.append(
                    fact(
                        h,
                        fid,
                        status="computed",
                        value=value,
                        unit="sections" if fid == "W05" else "count" if fid == "W06" else "%",
                        params=params,
                        recipe_id=fid + "-R2",
                        sources=[
                            {
                                "provider": "OpenStreetMap",
                                "dataset_version": graph.manifest.get("source_date"),
                                "dataset_sha256": graph.manifest.get("sha256"),
                                "url": "https://www.openstreetmap.org/copyright",
                                "license": "ODbL1.0",
                            }
                        ],
                        scope_note=(
                            "Computed mapped attributes conditional on saved estimated route. "
                            "Unknown unmapped obstacles/lighting retained; not a verified "
                            "accessibility/safety guarantee."
                        ),
                        recipe_attempts=attempts(
                            fid,
                            {fid + "-R1": "No full surveyed route obstacle/lighting inventory."},
                            fid + "-R2",
                        ),
                        hard_condition_eligible=False,
                    )
                )
            if dsm and dsm.get("ok"):
                dsmsources = [dsm_source(tile, raster_dir) for tile in dsm["tiles"]]
                facts.append(
                    fact(
                        h,
                        "W04",
                        status="estimate",
                        value=dsm,
                        unit="m",
                        params=params,
                        recipe_id="W04-D18-DSM",
                        sources=dsmsources,
                        depends_on=[{"kind": "route", "id": main["route_id"]}]
                        + [{"kind": "raster_tile", "id": tile} for tile in dsm["tiles"]],
                        scope_note=(
                            "Explicit D18 DSM surface estimate, outside mandatory conditions. "
                            "Canonical ground-DTM recipe failed; no conversion of DSM to "
                            "pavement elevations."
                        ),
                        recipe_attempts=attempts(
                            "W04",
                            {
                                "W04-R1": (
                                    "No ground DTM/engineering profile. Copernicus GLO30 is DSM, "
                                    "rejected as ground profile."
                                ),
                                "W04-R2": "No repeated surveyed or levelled route profile.",
                            },
                        ),
                        catalogue_recipe_not_satisfied=True,
                        hard_condition_eligible=False,
                    )
                )
            else:
                facts.append(
                    fact(
                        h,
                        "W04",
                        unknown_reason="Ground DTM/survey absent; DSM estimate unavailable",
                        params=params,
                        component_diagnostics=dsm,
                    )
                )
            facts.append(
                fact(
                    h,
                    "A01",
                    unknown_reason=(
                        "No zero-gap surveyed constraints: hotel exit, kerbs, width, "
                        "slope, surface, stairs and working lifts unverified. A route with"
                        " no mapped steps is insufficient."
                    ),
                    params={
                        **params,
                        "minimum_width_m": 0.9,
                        "maximum_incline_percent": 5,
                        "maximum_kerb_mm": 20,
                    },
                    component_diagnostics={
                        "known_steps_sections": sp["known_step_sections"],
                        "unverified_route": True,
                    },
                )
            )
            if main["target_kind"] == "beach":
                targets_out.append(
                    {
                        "hotel_id": h["hotel_id"],
                        "city_id": city,
                        "beach_id": main["target"]["beach_id"],
                        "beach_name": main["target"]["tags"].get("name"),
                        "beach_tags": main["target"]["tags"],
                        "beach_kind": "sea",
                        "marine_classification": main["target"]["marine_classification"],
                        "beach_geometry_wkb_projected": main["target"]["geometry"].wkb_hex,
                        "beach_geometry_wkb": gtransform(
                            graph.inv.transform, main["target"]["geometry"]
                        ).wkb_hex,
                        "route_id": main["route_id"],
                        "entry_candidate_id": main["target_id"],
                        "entry_lon_lat": main["coordinates"][-1],
                        "confirmed_public_entrance": False,
                        "source": osm_source(main["target"]["record"], graph.manifest),
                    }
                )
        else:
            for fid in ["W04", "W05", "W06", "A01", "Q03"]:
                facts.append(
                    fact(
                        h,
                        fid,
                        unknown_reason=(
                            "No selected connected route in cached graph; no absence or "
                            "accessibility conclusion."
                        ),
                    )
                )
        timings["W_A_Q03"] += time.perf_counter() - t0
        t0 = time.perf_counter()

        # Time-aware POI: evaluate true service hours, not generic near POI or bar hours.
        def match(t, fid):
            if fid == "L01":
                return t.get("amenity") == "pharmacy"
            if fid == "L02":
                return t.get("amenity") in {
                    "restaurant",
                    "cafe",
                    "fast_food",
                    "food_court",
                    "bar",
                    "pub",
                }
            if fid == "L03":
                return t.get("shop") == "supermarket"
            if fid == "F02":
                return (
                    t.get("healthcare:speciality") in {"paediatrics", "pediatrics"}
                    and t.get("emergency") == "yes"
                )
            if fid == "P04":
                return t.get("amenity") == "veterinary"
            if fid == "R04":
                return (
                    t.get("office") == "coworking" or t.get("amenity") == "coworking_space"
                ) and any(
                    t.get(k) in {"yes", "public", "day_pass"}
                    for k in ["coworking:day_pass", "day_pass", "access"]
                )
            return False

        for fid in ["L01", "L02", "L03", "F02", "P04", "R04"]:
            blocked = {r["poi_id"] for r in quality_exclusions if r["feature_id"] == fid}
            pool = [
                r
                for r in local_pois
                if match(r["tags"], fid)
                and p.distance(r["point"]) < 30000
                and r["sid"] not in blocked
            ]
            for trip in trips:
                date = trip["arrival_local"]
                states = [(r, opening_state(r, date, city, kitchen=fid == "L02")) for r in pool]
                opens = [r for r, s in states if s["state"] == "open"]
                rt = (
                    graph.choose(h, opens, fid)
                    if covered and opens
                    else {
                        "ok": False,
                        "reason": "no_confirmed_open_service_hours_in_cached_source_or_extent",
                    }
                )
                params = {
                    "arrival_local": date,
                    "timezone": TZ[city],
                    "candidate_search_radius_m": 30000,
                    "service": fid,
                    "kitchen_service_hours_required": fid == "L02",
                    "quality_exclusions": sorted(blocked),
                    "quality_exclusion_evidence": str(exclusions_file) if blocked else None,
                }
                if rt["ok"]:
                    target = rt["target"]
                    st = next(s for r, s in states if r["sid"] == target["sid"])
                    value = {
                        "distance_m": rt["length_estimate_m"],
                        "graph_length_m": rt["graph_length_m"],
                        "route_id": rt["route_id"],
                        "poi_id": target["sid"],
                        "poi_name": target["tags"].get("name"),
                        "opening_hours_evaluation": st,
                        "known_open_candidates": len(opens),
                        "unknown_hours_candidates": sum(s["state"] == "unknown" for r, s in states),
                        "nearest_scope": (
                            "nearest network route among all evaluated open eligible mapped "
                            "candidates within 30km air radius; unknown-hours POIs may be "
                            "nearer"
                        ),
                    }
                    facts.append(
                        fact(
                            h,
                            fid,
                            status="estimate",
                            value=value,
                            unit="m",
                            params=params,
                            recipe_id=fid + "-R2",
                            sources=[osm_source(target["record"], graph.manifest)],
                            scope_note=(
                                "Future declared recurring schedule plus research route estimate; "
                                "not guaranteed physical opening. Refresh hours/holiday exceptions"
                                " and verify entrances before hard constraint."
                            ),
                            recipe_attempts=attempts(
                                fid,
                                {
                                    fid + "-R1": (
                                        "No cached provider-confirmed specific-date service "
                                        "roster and "
                                        "surveyed graph."
                                    )
                                },
                                fid + "-R2",
                            ),
                            valid_to=None,
                            hard_condition_eligible=False,
                        )
                    )
                    poi_audit.append(
                        {
                            "hotel_id": h["hotel_id"],
                            "feature_id": fid,
                            "city_id": city,
                            "params": params,
                            "poi_id": target["sid"],
                            "poi_tags": target["tags"],
                            "hours": st,
                            "source": osm_source(target["record"], graph.manifest),
                            "route_id": rt["route_id"],
                        }
                    )
                else:
                    facts.append(
                        fact(
                            h,
                            fid,
                            unknown_reason=rt["reason"],
                            params=params,
                            component_diagnostics={
                                "eligible_source_pois": len(pool),
                                "open_source_pois": len(opens),
                                "closed_source_pois": sum(
                                    s["state"] == "closed" for r, s in states
                                ),
                                "unknown_source_hours": sum(
                                    s["state"] == "unknown" for r, s in states
                                ),
                                "not_inferred": (
                                    "No confirmed source candidate is not evidence "
                                    "no service exists."
                                ),
                            },
                        )
                    )
        # A dog_park map tag may describe a private husky attraction rather than a
        # designated, publicly admitted place where a visitor may walk their own animal.
        # Keep a route component, but do not substitute it for canonical P03.
        dog_pool = [
            r
            for r in graph.pois
            if r["tags"].get("leisure") == "dog_park"
            and r["tags"].get("access") not in {"private", "no", "customers"}
        ]
        dog_rt = (
            graph.choose(h, dog_pool, "mapped_dog_area_candidate")
            if covered
            else {"ok": False, "reason": "outside_cached_extent"}
        )
        dog_component = {
            "mapped_candidates": len(dog_pool),
            "target_entrance_access_and_rules_confirmed": False,
            "not_feature_value": (
                "leisure=dog_park and a route do not establish an "
                "OSM/hotelier-confirmed entrance, public admission or "
                "animal/leash/season rules for the requested animal."
            ),
        }
        if dog_rt["ok"]:
            dog_component.update(
                {
                    "route_id": dog_rt["route_id"],
                    "route_distance_estimate_m": dog_rt["length_estimate_m"],
                    "poi_id": dog_rt["target_id"],
                    "poi_name": dog_rt["target_name"],
                    "mapped_tags": dog_rt["target"]["tags"],
                    "source": osm_source(dog_rt["target"]["record"], graph.manifest),
                }
            )
            routes.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": dog_rt["coordinates"]}
                    if len(dog_rt["coordinates"]) > 1
                    else mapping(Point(dog_rt["coordinates"][0])),
                    "properties": {
                        "hotel_id": h["hotel_id"],
                        "city_id": city,
                        "route_id": dog_rt["route_id"],
                        "kind": "mapped_dog_area_candidate",
                        "target_id": dog_rt["target_id"],
                        "graph_length_m": dog_rt["graph_length_m"],
                        "hotel_snap_m": dog_rt["hotel_snap_m"],
                        "target_snap_m": dog_rt["target_snap_m"],
                        "node_ids": dog_rt["node_ids"],
                        "edge_way_ids": [e["sid"] for e in dog_rt["edges"]],
                        "validity": (
                            "component only: target entrance, public admission and "
                            "animal/leash/season rules unverified; not canonical P03"
                        ),
                    },
                }
            )
        else:
            dog_component["route_failure"] = dog_rt
        facts.append(
            fact(
                h,
                "P03",
                unknown_reason=(
                    "No OSM/hotelier-confirmed target entrance and "
                    "public-admission/animal/leash/season policy for mapped candidate;"
                    " OSM dog_park alone is insufficient."
                ),
                params={"target_kind": "designated_dog_area", "animal": "dog"},
                component_diagnostics=dog_component,
                recipe_attempts=attempts(
                    "P03",
                    {
                        "P03-R1": (
                            "No accepted official designated dog-area inventory and "
                            "permitted-animal policy in cached inputs."
                        ),
                        "P03-R2": (
                            "Mapped leisure=dog_park candidate lacks a confirmed target "
                            "entrance/public admission and requested animal/leash/season "
                            "rules; a separate official registry is not required by R2."
                        ),
                    },
                ),
            )
        )
        for fid, kind, pool in [
            (
                "F01",
                "playground",
                [
                    r
                    for r in graph.pois
                    if r["tags"].get("leisure") == "playground"
                    and r["tags"].get("access") not in {"private", "no", "customers"}
                ],
            )
        ]:
            rt = (
                graph.choose(h, pool, kind)
                if covered
                else {"ok": False, "reason": "outside_cached_extent"}
            )
            if rt["ok"]:
                facts.append(
                    fact(
                        h,
                        fid,
                        status="estimate",
                        value={
                            "distance_m": rt["length_estimate_m"],
                            "route_id": rt["route_id"],
                            "poi_id": rt["target_id"],
                            "poi_name": rt["target_name"],
                            "access_tag": rt["target"]["tags"].get("access"),
                            "age_or_dog_restrictions": {
                                k: v
                                for k, v in rt["target"]["tags"].items()
                                if k
                                in [
                                    "min_age",
                                    "max_age",
                                    "dog",
                                    "dog:conditional",
                                    "opening_hours",
                                    "access",
                                    "leash",
                                ]
                            },
                        },
                        unit="m",
                        recipe_id=fid + "-R2",
                        scope_note=(
                            "Mapped designated target and route estimate; verify "
                            "entrance/public access and age/leash/season rules. Ordinary green"
                            " space is not substituted for designated dog area."
                        ),
                        sources=[osm_source(rt["target"]["record"], graph.manifest)],
                        hard_condition_eligible=False,
                    )
                )
            else:
                facts.append(
                    fact(
                        h,
                        fid,
                        unknown_reason=(
                            "No eligible target/connected route in cached graph; absent "
                            "mapping is not absence of playground/dog area."
                        ),
                        component_diagnostics=rt,
                    )
                )
        timings["L_F_P_R04"] += time.perf_counter() - t0
        t0 = time.perf_counter()
    # Preserve the two requested seasonal contexts even for the same static graph route.
    # These are declared projections, not two independent physical observations.
    contextual = []
    route_features = {"W01", "W02", "W03", "W04", "W05", "W06", "A01", "Q03", "F01", "P03"}
    for f in facts:
        if f["feature_id"] not in route_features:
            contextual.append(f)
            continue
        for trip in trips:
            r = {
                **f,
                "params": {
                    **f["params"],
                    "trip_start": trip["start"],
                    "trip_end": trip["end"],
                    "seasonal_route_revalidation": "not performed; snapshot projection only",
                },
            }
            identity = json.dumps(
                [
                    r["city_id"],
                    r["hotel_id"],
                    r["feature_id"],
                    r["subject_level"],
                    r["subject_id"],
                    r["params"],
                ],
                sort_keys=True,
                ensure_ascii=False,
            )
            r["fact_id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
            contextual.append(r)
    facts = [normalize_route_fact(r) for r in contextual]
    write_jsonl(
        outdir / f"{city}_blocked_nodes.jsonl",
        [{"osm_node_id": n, **r} for n, r in sorted(graph.blocked_nodes.items())],
    )
    write_jsonl(outdir / f"{city}_beach_classifications.jsonl", graph.beach_classifications)
    write_jsonl(outdir / f"{city}_facts.jsonl", facts)
    write_jsonl(outdir / f"{city}_routes.jsonl", routes)
    write_jsonl(outdir / f"{city}_beach_targets.jsonl", targets_out)
    write_jsonl(outdir / f"{city}_poi_audit.jsonl", poi_audit)
    diag.update(
        {
            "total_seconds": time.perf_counter() - start,
            "family_seconds": dict(timings),
            "facts": len(facts),
            "unique_hotel_feature_pairs": len({(r["hotel_id"], r["feature_id"]) for r in facts}),
            "statuses": dict(collections.Counter(r["status"] for r in facts)),
            "result_bytes": sum(p.stat().st_size for p in outdir.glob(city + "_*jsonl")),
            "feature_counts": {
                fid: dict(collections.Counter(r["status"] for r in facts if r["feature_id"] == fid))
                for fid in OWN
            },
            "graph_license": (
                "ODbL1.0; attribution/share-alike obligations; source ages and extent retained"
            ),
        }
    )

    return facts, diag, outputs


@register_calculator
class RoutePoiCalculator:
    id = "route.poi"
    version = VERSION
    feature_ids = tuple(OWN)

    def __init__(self):
        self.outputs = {}
        self.diagnostics = {}

    @staticmethod
    def unknown(subjects: Sequence[Subject], city: str, reason: str) -> list[Fact]:
        return [
            Fact(
                fact_id=make_fact_id(s.hotel_id, s.subject_id, fid, {}, VERSION),
                hotel_id=s.hotel_id,
                city_id=city,
                subject_level="hotel",
                subject_id=s.subject_id,
                feature_id=fid,
                status="unknown",
                unknown_reason=reason,
                algorithm_version=VERSION,
                hard_condition_eligible=False,
            )
            for s in subjects
            for fid in OWN
        ]

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        self.outputs = {}
        self.diagnostics = {}
        trips = ctx.params.get("trips", TRIPS)
        if not trips:
            raise ValueError("Нужно хотя бы одно окно поездки")
        for trip in trips:
            if date.fromisoformat(trip["end"]) <= date.fromisoformat(trip["start"]):
                raise ValueError("Дата выезда должна быть позже даты заезда")
            datetime.fromisoformat(trip["arrival_local"])
        valid = [
            s
            for s in subjects
            if s.latitude is not None
            and s.longitude is not None
            and math.isfinite(s.latitude)
            and math.isfinite(s.longitude)
            and -90 <= s.latitude <= 90
            and -180 <= s.longitude <= 180
        ]
        valid_objects = {id(s) for s in valid}
        invalid = self.unknown(
            [s for s in subjects if id(s) not in valid_objects],
            ctx.city_id,
            "hotel_coordinates_missing_or_invalid",
        )
        if not valid:
            return invalid
        dataset = ctx.datasets.get("osm_city")
        if dataset is None or not (dataset.path / "manifest.json").is_file():
            return self.unknown(valid, ctx.city_id, "osm_city_dataset_missing") + invalid
        manifest = json.loads((dataset.path / "manifest.json").read_text())
        manifest["sha256"] = manifest["source_sha256"]
        records = iter_jsonl(dataset.path / "records.jsonl.gz")
        if ctx.params.get("compat_v9"):
            # Только контрольный повтор старой сцены. Полный расчёт этим не ограничивается.
            bbox = (39.15, 43.35, 40.5, 44.05)
            manifest["bbox"] = bbox

            def old_scene(rows):
                for record in rows:
                    geom = wkb.loads(record["geometry_wkb"], hex=True)
                    if geom.geom_type == "Point":
                        x, y = geom.x, geom.y
                    elif geom.geom_type == "MultiPolygon":
                        x, y = geom.geoms[0].exterior.coords[0]
                    elif geom.geom_type == "Polygon":
                        x, y = geom.exterior.coords[0]
                    else:
                        x, y = geom.coords[0]
                    if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                        yield record

            records = old_scene(records)
        try:
            graph = ctx.params.get("shared_graph")
            if graph is None:
                graph = PedestrianGraph(
                    ctx.city_id, records, manifest, compact=ctx.params.get("compact", False)
                )
            elif graph.city != ctx.city_id or graph.manifest.get("sha256") != manifest["sha256"]:
                raise ValueError("Shared graph city/source mismatch")
        except ValueError as exc:
            if str(exc) != "empty_pedestrian_graph":
                raise
            return self.unknown(valid, ctx.city_id, "empty_pedestrian_graph") + invalid
        selected_routes = {}
        original_route = graph.route

        def capture_route(hotel, target, kind):
            route = original_route(hotel, target, kind)
            if route.get("ok"):
                selected_routes[route["route_id"]] = {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": route["coordinates"]}
                    if len(route["coordinates"]) > 1
                    else mapping(Point(route["coordinates"][0])),
                    "properties": {
                        "hotel_id": hotel["hotel_id"],
                        "route_id": route["route_id"],
                        "kind": kind,
                        "node_ids": route["node_ids"],
                        "edge_way_ids": [edge["sid"] for edge in route["edges"]],
                        "graph_length_m": route["graph_length_m"],
                        "dataset_sha256": manifest["source_sha256"],
                        "status": "estimate",
                        "entrances_verified": False,
                    },
                }
            return route

        graph.route = capture_route
        hotels = [
            {
                "hotel_id": s.hotel_id,
                "city_id": ctx.city_id,
                "latitude": s.latitude,
                "longitude": s.longitude,
            }
            for s in valid
        ]
        try:
            rows, self.diagnostics, self.outputs = _calculate(
                ctx.city_id,
                hotels,
                graph,
                ctx.params.get("building_matches", []),
                Path(ctx.params.get("raster_dir", dataset.path / "raster_tiles")),
                ctx.params.get("calculated_at"),
                trips,
            )
        finally:
            graph.route = original_route
        self.outputs[f"{ctx.city_id}_all_selected_routes.jsonl"] = list(selected_routes.values())
        blocked = set(graph.blocked_nodes)
        bad = []
        for output in self.outputs.values():
            for row in output:
                if row.get("type") == "Feature":
                    hit = blocked.intersection(row.get("properties", {}).get("node_ids", []))
                    if hit:
                        bad.append(sorted(hit))
        if bad:
            raise ValueError("Route contains blocked OSM node")
        self.diagnostics["blocked_node_contacts"] = 0
        result = []
        for row in rows:
            if row["feature_id"] not in OWN:
                continue
            row = move_provenance_params(row)
            row["algorithm_version"] = VERSION
            route_id = (row.get("value") or {}).get("route_id") or row["params"].get("route_id")
            if route_id:
                row["depends_on"] = row.get("depends_on", []) + [{"kind": "route", "id": route_id}]
            row["fact_id"] = make_fact_id(
                row["hotel_id"],
                row["subject_id"],
                row["feature_id"],
                row["params"],
                VERSION + ":" + manifest["source_sha256"],
            )
            result.append(Fact.model_validate(row))
        return result + invalid
