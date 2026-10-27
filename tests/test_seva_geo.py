"""Контракты этапов 3–6: синтетическая геометрия, доступ, часы и полный конвейер."""

import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest


@pytest.fixture(scope="module", autouse=True)
def builder_dependencies():
    for name in ("numpy", "scipy", "shapely", "pyproj", "osmium", "rasterio"):
        pytest.importorskip(name)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r) + "\n" for r in rows).encode()
    path.write_bytes(gzip.compress(payload, mtime=0))


@pytest.fixture
def geography_root(tmp_path):
    from shapely.geometry import box, mapping

    root = tmp_path / "osm"
    for city, geom in (
        ("moscow", box(37.5, 55.5, 37.8, 55.8)),
        ("sochi", box(39.5, 43.5, 39.9, 43.9)),
    ):
        folder = root / city
        folder.mkdir(parents=True)
        feature = {
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {"osm_id": "relation/test"},
        }
        doc = {
            "union_geometry": mapping(geom),
            "features": [feature],
            "source_sha256": "a" * 64,
            "source_date": "2026-10-22T20:21:33Z",
        }
        (folder / "boundary.geojson").write_text(json.dumps(doc))
    zones = [
        {"type": "Feature", "geometry": mapping(geom), "properties": {"name": name}}
        for name, geom in (
            ("Адлер", box(39.5, 43.5, 39.9, 43.9)),
            ("Сириус", box(39.6, 43.6, 39.7, 43.7)),
        )
    ]
    (root / "sochi/localities.geojson").write_text(json.dumps({"features": zones}))
    return root


def test_city_boundary_and_sirius_precedence(geography_root):
    from connectors.city_geography import CityGeography

    geography = CityGeography(geography_root)
    assert geography.city_for(37.5, 55.5) == "moscow"  # Граница включена.
    assert geography.city_for(37.49, 55.5) is None
    assert geography.subzone_for(39.65, 43.65)[0] == "sirius"
    assert geography.subzone_for(39.8, 43.8)[0] == "adler"
    assert geography.subzone_for(39.99, 43.99)[0] == "other_greater_sochi"


def test_every_subzone_code_has_a_guest_name():
    """В пакете `subzone` — название для гостя, код — `subzone_id`; кода гостю не показываем."""
    from connectors.city_geography import OTHER_SUBZONE, SUBZONE_NAMES, SUBZONES

    assert set(SUBZONES.values()) | {OTHER_SUBZONE} == set(SUBZONE_NAMES)
    assert SUBZONE_NAMES[OTHER_SUBZONE] == "Большой Сочи"
    assert all(name[0].isupper() and "_" not in name for name in SUBZONE_NAMES.values())


def test_osm_extract_retains_crossing_way_and_pure_gate(tmp_path):
    from connectors.osm_pbf import extract_city
    from core.packs import iter_jsonl

    node_rows = [
        (1, 36.0, 55.5, ""),
        (2, 38.1, 55.5, ""),
        (12, 37.4, 55.5, ""),
        (13, 37.5, 55.5, '<tag k="barrier" v="gate"/><tag k="locked" v="yes"/>'),
        (14, 37.6, 55.5, ""),
        (30, 37.0, 55.2, ""),
        (31, 38.0, 55.2, ""),
        (32, 38.0, 56.0, ""),
        (33, 37.0, 56.0, ""),
    ]
    nodes = "".join(
        f'<node id="{i}" version="1" lon="{x}" lat="{y}">{tags}</node>'
        for i, x, y, tags in node_rows
    )
    source = tmp_path / "test.osm"
    source.write_text(f"""<osm version="0.6" generator="synthetic-test">{nodes}
      <way id="10" version="1"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
      <way id="11" version="1"><nd ref="12"/><nd ref="13"/><nd ref="14"/>
        <tag k="highway" v="footway"/></way>
      <way id="100" version="1"><nd ref="30"/><nd ref="31"/><nd ref="32"/>
        <nd ref="33"/><nd ref="30"/></way>
      <relation id="102269" version="1"><member type="way" ref="100" role="outer"/>
        <tag k="type" v="boundary"/><tag k="boundary" v="administrative"/>
        <tag k="name" v="Москва"/><tag k="admin_level" v="4"/></relation></osm>""")
    out = tmp_path / "extract"
    manifest = extract_city(source, out, "moscow")
    rows = list(iter_jsonl(out / "records.jsonl.gz"))
    crossing = next(r for r in rows if r["osm_type"] == "way" and r["osm_id"] == 10)
    assert crossing["nodes"][0][1] == 36.0  # Первая вершина за конвертом, линия не потерялась.
    assert any(r["osm_type"] == "node" and r["osm_id"] == 13 for r in rows)
    assert manifest["counts"]["restriction_nodes"] >= 1
    assert manifest["boundary_relation_ids"] == [102269]
    assert extract_city(source, out, "moscow") == manifest  # Повторная проверка хешей.


@pytest.mark.parametrize(
    "tags,allowed",
    [
        ({"highway": "footway"}, True),
        ({"highway": "footway", "access": "private"}, False),
        ({"highway": "footway", "access": "private", "foot": "yes"}, True),
        ({"highway": "footway", "foot:conditional": "yes @ (Mo-Fr)"}, False),
        ({"highway": "motorway"}, False),
        ({"highway": "cycleway", "foot": "designated"}, True),
    ],
)
def test_pedestrian_edge_policy(tags, allowed):
    from calculators.route_graph import allowed as edge_allowed

    assert edge_allowed(tags) is allowed


def way(oid, nodes, tags):
    from shapely.geometry import LineString

    return {
        "osm_type": "way",
        "osm_id": oid,
        "tags": tags,
        "nodes": nodes,
        "geometry_wkb": LineString([(x, y) for _, x, y in nodes]).wkb_hex,
        "version": 1,
        "timestamp": "2026-10-22T00:00:00Z",
    }


def node(oid, x, y, tags):
    from shapely.geometry import Point

    return {
        "osm_type": "node",
        "osm_id": oid,
        "tags": tags,
        "nodes": None,
        "geometry_wkb": Point(x, y).wkb_hex,
        "version": 1,
        "timestamp": "2026-10-22T00:00:00Z",
    }


MANIFEST = {
    "bbox": [39, 43, 41, 45],
    "source_date": "2026-10-22T20:21:33Z",
    "source_sha256": "a" * 64,
    "sha256": "a" * 64,
}


def test_gate_node_blocks_route_and_geometry_crossing_does_not_join():
    from shapely.geometry import Point

    from calculators.route_graph import PedestrianGraph

    nodes = [[1, 39.72, 43.58], [2, 39.721, 43.58], [3, 39.722, 43.58]]
    graph = PedestrianGraph(
        "sochi",
        [
            way(10, nodes, {"highway": "footway"}),
            node(2, 39.721, 43.58, {"barrier": "gate", "locked": "yes"}),
        ],
        MANIFEST,
    )
    hotel = {"hotel_id": "1", "longitude": 39.72, "latitude": 43.58}
    target = {
        "sid": "test",
        "point": graph.project_hotel({"longitude": 39.722, "latitude": 43.58}),
        "tags": {},
    }
    assert 2 not in graph.nids and not graph.route(hotel, target, "stop")["ok"]
    horizontal = way(20, [[1, 39.72, 43.58], [2, 39.722, 43.58]], {"highway": "footway"})
    vertical = way(21, [[3, 39.721, 43.579], [4, 39.721, 43.581]], {"highway": "footway"})
    graph = PedestrianGraph("sochi", [horizontal, vertical], MANIFEST)
    target["point"] = Point(*graph.tr.transform(39.721, 43.581))
    assert not graph.route(hotel, target, "stop")["ok"]


def test_steps_and_zero_length_lighting():
    from calculators.route_geo import normalize_route_fact
    from calculators.route_graph import lighting_metrics, steps_metrics

    edges = [
        {"length_m": 3, "tags": {"highway": "steps", "lit": "yes"}},
        {"length_m": 2, "tags": {"highway": "steps"}},
        {"length_m": 5, "tags": {"highway": "footway", "lit": "no"}},
    ]
    assert steps_metrics(edges)["known_step_sections"] == 1
    assert lighting_metrics(edges)["unknown_percent"] == 20
    row = {
        "hotel_id": "1",
        "city_id": "sochi",
        "feature_id": "Q03",
        "status": "computed",
        "value": lighting_metrics([]),
        "params": {},
        "subject_level": "hotel",
        "subject_id": "hotel:1",
    }
    result = normalize_route_fact(row)
    assert result["status"] == "unknown" and result["value"] is None


def test_compact_graph_preserves_paths_lengths_and_restrictions():
    import copy

    from calculators.route_graph import PackedNodes, PedestrianGraph, lighting_metrics

    rows = [
        way(
            1,
            [[1, 39.72, 43.58], [2, 39.721, 43.58], [3, 39.722, 43.58]],
            {"highway": "footway", "lit": "yes"},
        ),
        node(9, 39.72, 43.58, {"access": "private"}),
    ]
    a = PedestrianGraph("sochi", copy.deepcopy(rows), MANIFEST)
    b = PedestrianGraph("sochi", copy.deepcopy(rows), MANIFEST, compact=True)
    assert a.nids == b.nids and (a.A != b.A).nnz == 0
    assert a.blocked_nodes == b.blocked_nodes
    h = {"hotel_id": "1", "longitude": 39.72, "latitude": 43.58}
    target = {
        "sid": "end",
        "point": a.project_hotel({"longitude": 39.722, "latitude": 43.58}),
        "tags": {},
    }
    x, y = a.route(h, target, "stop"), b.route(h, target, "stop")
    for key in ("node_ids", "coordinates", "length_estimate_m", "route_id"):
        assert x[key] == y[key]
    assert lighting_metrics(x["edges"]) == lighting_metrics(y["edges"])
    with pytest.raises(ValueError, match="exact float64"):
        PackedNodes([[2**53 + 1, 0, 0]])


@pytest.mark.parametrize(
    "tags,kitchen,state",
    [
        ({"opening_hours": "24/7"}, False, "open"),
        ({"opening_hours": "Mo-Fr 09:00-18:00"}, False, "closed"),
        ({"opening_hours": "garbage"}, False, "unknown"),
        ({"opening_hours": "24/7"}, True, "unknown"),
        ({"opening_hours:kitchen": "24/7"}, True, "open"),
        ({}, False, "unknown"),
    ],
)
def test_hours_are_date_and_service_specific(tags, kitchen, state):
    from calculators.poi_hours import opening_state

    result = opening_state(tags, "2026-11-14T02:00:00", timezone="Europe/Moscow", kitchen=kitchen)
    assert result["state"] == state


def test_calculator_outputs_facts_and_auditable_routes(tmp_path):
    from calculators.route_geo import OWN, RoutePoiCalculator
    from core.plugins import CityContext, DatasetRef, Subject

    raw = [
        way(
            10,
            [[1, 39.72, 43.58], [2, 39.7203, 43.58], [3, 39.7206, 43.58]],
            {"highway": "footway", "lit": "yes"},
        ),
        node(2, 39.7203, 43.58, {"highway": "bus_stop"}),
        node(3, 39.7206, 43.58, {"amenity": "pharmacy", "opening_hours": "24/7"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", raw)
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    context = CityContext(
        "sochi",
        "Europe/Moscow",
        tmp_path,
        datasets={"osm_city": DatasetRef("osm_city", tmp_path, "OpenStreetMap")},
    )
    subject = Subject("1", "hotel", "hotel:1", latitude=43.58, longitude=39.72)
    calc = RoutePoiCalculator()
    facts = calc.run([subject], context)
    assert {f.feature_id for f in facts} == set(OWN)
    assert all(
        f.status == "estimate" and not f.usable_in_hard for f in facts if f.feature_id == "W02"
    )
    assert all(f.status == "unknown" for f in facts if f.feature_id in {"N02", "N03", "W04", "L02"})
    assert all(f.status == "estimate" for f in facts if f.feature_id == "L01")
    assert calc.outputs["sochi_all_selected_routes.jsonl"]
    assert calc.diagnostics["blocked_node_contacts"] == 0
    again = calc.run([subject], context)
    assert [f.model_dump() for f in facts] == [f.model_dump() for f in again]
    custom = CityContext(
        "sochi",
        "Europe/Moscow",
        tmp_path,
        datasets=context.datasets,
        params={
            "trips": [
                {"start": "2026-10-27", "end": "2026-10-28", "arrival_local": "2026-10-27T02:00:00"}
            ],
        },
    )
    changed = calc.run([subject], custom)
    assert {f.params["arrival_local"] for f in changed if f.feature_id == "L01"} == {
        "2026-10-27T02:00:00"
    }
    assert {f.params["trip_start"] for f in changed if f.feature_id == "W02"} == {"2026-10-27"}


def test_missing_coordinates_and_empty_graph_return_unknown(tmp_path):
    from calculators.route_geo import OWN, RoutePoiCalculator
    from core.plugins import CityContext, DatasetRef, Subject

    calc = RoutePoiCalculator()
    valid = Subject("1", "hotel", "hotel:1", latitude=43.58, longitude=39.72)
    invalid = Subject("2", "hotel", "hotel:2")
    context = CityContext("sochi", "Europe/Moscow", tmp_path)
    assert all(f.unknown_reason == "osm_city_dataset_missing" for f in calc.run([valid], context))
    assert len(calc.run([invalid], context)) == len(OWN)
    write_rows(tmp_path / "records.jsonl.gz", [])
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    context = CityContext(
        "sochi",
        "Europe/Moscow",
        tmp_path,
        datasets={"osm_city": DatasetRef("osm_city", tmp_path, "OpenStreetMap")},
    )
    assert all(f.unknown_reason == "empty_pedestrian_graph" for f in calc.run([valid], context))


def test_regression_detects_missing_metric_and_new_building_mapping():
    from pipeline.calculate_city import regression

    old = {
        "hotel_id": "1",
        "feature_id": "N02",
        "subject_level": "building",
        "subject_id": "way/1",
        "status": "computed",
        "value": {"distance_m": 10},
    }
    new = old | {"value": {}}
    assert regression([old], [new])["counts"] == {"metric_missing": 1}
    old = old | {
        "subject_level": "hotel",
        "subject_id": "hotel:1",
        "status": "unknown",
        "value": None,
    }
    assert regression([old], [new])["counts"] == {"status_changed": 1}


def test_changed_coordinate_explanation_requires_same_route():
    from pipeline.calculate_city import explain_changes

    old = [
        {"hotel_id": "1", "feature_id": "W02", "value": {"route_id": "same"}},
        {"hotel_id": "1", "feature_id": "C01", "value": 22.0},
    ]
    new = [{"hotel_id": "1", "feature_id": "W02", "value": {"route_id": "same"}}]
    cohort = [{"hotel_id": "1", "longitude": 39.7, "latitude": 43.5}]
    hotels = [{"id": "1", "longitude": 39.71, "latitude": 43.5}]
    change = {"key": ["1", "W02"], "reason": "metric_changed"}
    report = {"differences": [change]}
    assert explain_changes(report, old, new, cohort, hotels)["1"]["new_coordinates"] == [
        39.71,
        43.5,
    ]
    assert change["explanation"] == "snapshot_coordinates_changed_same_route"
    new[0]["value"]["route_id"] = "different"
    explain_changes(report, old, new, cohort, hotels)
    assert change["explanation"] == "requires_input_review"


def test_building_match_requires_full_address_and_unique_geometry(tmp_path):
    from shapely.geometry import box, mapping

    from pipeline.calculate_city import building_matches

    building = {
        "id": "way/1",
        "city_id": "sochi",
        "geometry": mapping(box(39.7, 43.5, 39.8, 43.6)),
        "tags": {"addr:street": "Тестовая улица", "addr:housenumber": "1Б"},
    }
    hotel = {"id": "1", "longitude": 39.75, "latitude": 43.55, "address": "Тестовая улица, 1"}
    path = tmp_path / "buildings.jsonl.gz"
    write_rows(path, [building])
    assert not building_matches([hotel], [], tmp_path)[0]  # 1 ≠ 1Б.
    hotel["address"] = "Тестовая улица, 1Б"
    assert len(building_matches([hotel], [], tmp_path)[0]) == 1
    write_rows(path, [building, building | {"id": "way/2"}])
    assert not building_matches([hotel], [], tmp_path)[0]  # Два контура — не угадывать.


def test_catalog_filters_coordinates_preserves_v9_and_recounts(geography_root, tmp_path):
    from pipeline.build_packs import build_catalog, pack_parts, write_pack

    v9 = tmp_path / "v9"
    for city, hid, lon, lat in (("moscow", "2", 37.7, 55.7), ("sochi", "4", 39.8, 43.8)):
        hotel = {
            "id": hid,
            "city_id": city,
            "name": "Тест v9",
            "address": "Тестовая 1",
            "longitude": lon,
            "latitude": lat,
            "source": {},
        }
        fact = {
            "fact_id": hid,
            "hotel_id": hid,
            "city_id": city,
            "subject_level": "hotel",
            "subject_id": f"hotel:{hid}",
            "feature_id": "W02",
            "status": "unknown",
            "unknown_reason": "test",
        }
        write_pack(
            v9,
            city,
            {
                "hotels": [hotel],
                "facts": [fact],
                "coverage": [{"city_id": city, "feature_id": "W02", "n_hotels": 1}],
            },
            sources=[],
            built_at=datetime(2026, 10, 24, tzinfo=UTC),
            code_version="test",
            notes="Synthetic",
        )
    snapshot = tmp_path / "snapshot"
    pages = []
    for city, hid, lon, lat in (
        ("moscow", "1", 37.6, 55.6),
        ("moscow", "5", 30.0, 50.0),
        ("sochi", "6", 39.65, 43.65),
    ):
        raw = {
            "props": {
                "pageProps": {
                    "hotel": {
                        "masterId": hid,
                        "name": "Синтетический отель",
                        "location": {"address": "Тестовая 1", "longitude": lon, "latitude": lat},
                        "facts": {},
                        "roomGroups": [{"rgHash": "room", "nameStruct": {"mainName": "Стандарт"}}],
                    }
                }
            }
        }
        payload = json.dumps(raw).encode()
        sha = hashlib.sha256(payload).hexdigest()
        relative = f"next/{sha}.json.gz"
        path = snapshot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(payload))
        pages.append(
            {
                "city": city,
                "status": 200,
                "url": f"https://ostrovok.ru/hotel/russia/{city}/mid{hid}/test/",
                "next_file": relative,
                "sha256": sha,
                "fetched_at": "2026-10-24T00:00:00+00:00",
            }
        )
    (snapshot / "manifests").mkdir()
    (snapshot / "manifests/pages.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pages))
    out = tmp_path / "catalog"
    report = build_catalog(snapshot, geography_root, out, v9_packs=v9)
    assert report["cities"]["moscow"]["hotels"] == 2
    assert report["cities"]["sochi"]["hotels"] == 2
    assert any(r["reason"] == "outside_focus_boundaries" for r in report["excluded"])
    assert {h["id"] for h in pack_parts(out / "moscow")["hotels"]} == {"1", "2"}
    for city in ("moscow", "sochi"):
        row = pack_parts(out / city)["coverage"][0]
        assert row["n_hotels"] == row["unknown"] == 2
    zones = {h["id"]: (h["subzone"], h["subzone_id"]) for h in pack_parts(out / "sochi")["hotels"]}
    assert zones == {"6": ("Сириус", "sirius"), "4": ("Адлер", "adler")}
    assert {h["subzone_id"] for h in pack_parts(out / "moscow")["hotels"]} == {None}
