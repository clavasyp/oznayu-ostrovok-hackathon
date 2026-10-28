import copy
import json

import pytest


@pytest.fixture(scope="module", autouse=True)
def builder():
    for name in ("shapely", "pyproj", "numpy", "scipy", "rasterio"):
        pytest.importorskip(name)


def test_facades_preserve_ids_when_ring_start_or_direction_changes():
    from shapely.geometry import Polygon, mapping

    from calculators.route_facades import facade_rows

    coords = [(37.6, 55.7), (37.601, 55.7), (37.601, 55.701), (37.6, 55.701)]
    building = {"id": "way/1", "geometry": mapping(Polygon(coords))}
    a = facade_rows(building)
    for changed in (coords[2:] + coords[:2], list(reversed(coords))):
        b = facade_rows(building | {"geometry": mapping(Polygon(changed))})
        assert {f["facade_id"] for f in a} == {f["facade_id"] for f in b}
    assert {f["side"] for f in a} == {"N", "E", "S", "W"}
    assert all(f["is_courtyard"] is None for f in a)
    hole = [(37.6002, 55.7002), (37.6008, 55.7002), (37.6008, 55.7008), (37.6002, 55.7008)]
    rows = facade_rows(building | {"geometry": mapping(Polygon(coords, [hole]))})
    assert sum(f["is_courtyard"] is True for f in rows) == 4
    assert all(f["length_m"] > 0 for f in rows)


def test_placement_adapter_uses_only_evidenced_aliases_and_all_walls_of_a_side():
    from agents.hotelier.placement import PlacementCompact, to_placements
    from core.placement_catalog import placement_maps, side_segments

    links = [
        {
            "hotel_id": "h",
            "building_id": "way/1",
            "evidence": {
                "hotel_aliases": [
                    {"name": "корпус 2", "status": "hotelier", "source": {"answer_id": "real"}},
                    {"name": "главный", "status": "proposed", "source": {"url": "candidate"}},
                ]
            },
        }
    ]
    facades = [
        {
            "building_id": "way/1",
            "facade_id": "west",
            "side": "W",
            "is_courtyard": None,
            "length_m": 12.0,
        }
    ]
    aliases, sides = placement_maps(links, facades, "h")
    assert aliases["корпус 2"] == "way/1" and "главный" not in aliases
    compact = PlacementCompact(
        placements=[
            {
                "building": "корпус 2",
                "floor_from": "3",
                "floor_to": "5",
                "all_floors": False,
                "sides": ["W"],
                "corner": False,
                "quote": "корпус 2, 3–5, запад",
            }
        ],
        completeness="complete",
        guarantee="all_guaranteed",
        unclear=[],
    )

    def walls():
        return {("way/1", s): ids for s, ids in side_segments(facades, "way/1").items()}

    placed, missing = to_placements(compact, aliases, walls())
    assert not missing and [p["floor"] for p in placed["items"]] == [3, 4, 5]
    # D41-3: вторая стена на запад — номер на одной из двух, без уточнения; отрезок короче
    # 3 м — не стена.
    facades.append(facades[0] | {"facade_id": "another-west"})
    facades.append(facades[0] | {"facade_id": "kink", "length_m": 2.5})
    assert ("way/1", "W") not in placement_maps(links, facades, "h")[1]
    placed, missing = to_placements(compact, aliases, walls())
    assert not missing
    assert {(p["floor"], tuple(p["windows"])) for p in placed["items"]} == {
        (f, (w,)) for f in (3, 4, 5) for w in ("another-west", "west")
    }


def test_d34_keeps_all_categorized_places_in_radius_without_pairs(tmp_path):
    """Все места в 1500 м, не top-k; пар «отель → место» в пакете нет — их считает PostGIS (D41).
    Скамейка, парковка и сам отель без категории в пакет не идут; аптека — категория L01."""
    from calculators.route_facades import GEOD
    from pipeline.nearby_tables import build
    from tests.test_seva_geo import MANIFEST, node, write_rows

    rows = []
    for index, distance in enumerate((0, 100, 200, 300, 400, 1499, 1501)):
        x, y, _ = GEOD.fwd(37.6, 55.7, 90, distance)
        rows.append(node(index + 1, x, y, {"amenity": "restaurant", "cuisine": "italian;pizza"}))
    rows.extend(
        node(100 + i, 37.6 + 0.001 * i, 55.7, {"railway": "station", "station": "subway"})
        for i in range(4)
    )
    rows += [
        node(200, 37.6, 55.7001, {"amenity": "bench"}),
        node(201, 37.6, 55.7002, {"amenity": "parking"}),
        node(202, 37.6, 55.7003, {"tourism": "hotel", "name": "Сам отель"}),
        node(203, 37.6, 55.7004, {"amenity": "pharmacy"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", rows)
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    parts = {"hotels": [{"id": "h", "latitude": 55.7, "longitude": 37.6}]}
    report = build(parts, tmp_path, "moscow")
    assert "hotel_places" not in parts
    assert report["places"] == 7 and report["hotel_place_pairs"] == 7
    by_id = {p["id"]: p["categories"] for p in parts["places"]}
    assert by_id.pop("osm:node/203") == ["L01"]
    assert len(by_id) == 6 and all("italian" in c and "L02" in c for c in by_id.values())
    assert len(parts["hotel_stations"]) == 3 and parts["transit_times"] == []
    assert report["transit_time_status"] == "unknown"


def _area(oid, polygon, tags):
    return {
        "osm_type": "way",
        "osm_id": oid,
        "tags": tags,
        "nodes": None,
        "geometry_wkb": polygon.wkb_hex,
        "version": 1,
        "timestamp": "2026-10-22T00:00:00Z",
    }


def _square(lon, lat, east_m, size_m):
    """Квадрат size_m × size_m, западный край в east_m к востоку от точки."""
    from shapely.geometry import box

    from calculators.route_facades import GEOD

    west = GEOD.fwd(lon, lat, 90, east_m)[0]
    east = GEOD.fwd(lon, lat, 90, east_m + size_m)[0]
    north = GEOD.fwd(lon, lat, 0, size_m / 2)[1]
    south = GEOD.fwd(lon, lat, 180, size_m / 2)[1]
    return box(west, south, east, north)


def test_d34_park_gets_edge_points_and_distance_to_edge(tmp_path):
    """Километровый парк в 200 м: точка внутри — в ~700 м, а ближайшая точка края — в 200 м
    (не дальше чем на половину шага). Зелень двора без названия и закрытый парк — не парки."""
    from calculators.route_facades import GEOD
    from pipeline.nearby_tables import EDGE_SPACING_M, build
    from tests.test_seva_geo import MANIFEST, node, write_rows

    hotel = (37.6, 55.7)
    rows = [
        _area(1, _square(*hotel, 200, 1000), {"leisure": "park", "name": "Большой парк"}),
        _area(2, _square(*hotel, -100, 40), {"leisure": "park"}),  # двор, 0,16 га
        _area(3, _square(*hotel, 300, 100), {"leisure": "park"}),  # без названия, 1 га
        _area(4, _square(*hotel, 400, 30), {"leisure": "park", "name": "Сквер"}),
        _area(5, _square(*hotel, 500, 200), {"leisure": "park", "access": "private"}),
        _area(6, _square(*hotel, 600, 200), {"leisure": "garden"}),  # сад без названия
        _area(7, _square(*hotel, 700, 200), {"leisure": "garden", "name": "Аптекарский огород"}),
        node(8, 37.601, 55.7, {"aerialway": "station", "name": "Нижняя станция"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", rows)
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    parts = {"hotels": [{"id": "h", "longitude": hotel[0], "latitude": hotel[1]}]}
    report = build(parts, tmp_path, "moscow")
    by_id = {p["id"]: p for p in parts["places"]}
    objects = {k for k in by_id if "#" not in k}
    assert objects == {f"osm:way/{i}" for i in (1, 3, 4, 7)} | {"osm:node/8"}
    assert by_id["osm:node/8"]["categories"] == ["ski_lift"]
    edges = [p for p in parts["places"] if p["id"].startswith("osm:way/1#edge")]
    assert edges and all(p["categories"] == ["park"] for p in edges)
    info = edges[0]["source"]["geometry"]
    assert info["role"] == "edge" and info["object"] == "osm:way/1"
    assert info["spacing_m"] == EDGE_SPACING_M and 3_900 < info["edge_length_m"] < 4_100
    assert by_id["osm:way/1"]["source"]["geometry"]["role"] == "representative_point"
    assert 0.99e6 < by_id["osm:way/1"]["source"]["geometry"]["area_m2"] < 1.01e6

    def distance(p):
        return GEOD.inv(*hotel, p["longitude"], p["latitude"])[2]

    assert distance(by_id["osm:way/1"]) > 600  # точка внутри обманывает
    nearest = min(distance(p) for p in edges)
    assert 200 <= nearest <= 200 + EDGE_SPACING_M / 2
    assert all(distance(p) <= 1500 for p in edges)  # точки края — только в 1500 м от отелей
    assert report["edge_objects"] == 4 and report["edge_rows"] == len(
        [k for k in by_id if "#edge" in k]
    )
    assert report["places"] == 5 and report["place_rows"] == len(parts["places"])
    assert [p["id"] for p in parts["places"]] == sorted(by_id)


def test_d34_region_drops_places_and_stations_beyond_border(tmp_path):
    """Срез Сочи захватывает Абхазию: станция за границей субъекта не становится ближайшей."""
    from shapely.geometry import box, mapping

    from pipeline.nearby_tables import build
    from tests.test_seva_geo import MANIFEST, node, write_rows

    rows = [
        node(1, 40.000, 43.40, {"railway": "station", "name": "Весёлое"}),
        node(2, 40.013, 43.39, {"railway": "station", "name": "Ԥсоу"}),
        node(3, 40.012, 43.39, {"amenity": "pharmacy", "name": "Аптека за границей"}),
        node(4, 39.999, 43.40, {"amenity": "pharmacy"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", rows)
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST))
    region = {
        "features": [{"type": "Feature", "geometry": None, "properties": {"osm_id": "r/1"}}],
        "union_geometry": mapping(box(39.0, 43.2, 40.005, 44.6)),
    }
    (tmp_path / "region.geojson").write_text(json.dumps(region))
    parts = {"hotels": [{"id": "h", "longitude": 40.004, "latitude": 43.395}]}
    report = build(parts, tmp_path, "sochi")
    assert [s["name"] for s in parts["transit_stations"]] == ["Весёлое"]
    assert [p["id"] for p in parts["places"]] == ["osm:node/4"]
    assert {(s["station_id"], s["rank"]) for s in parts["hotel_stations"]} == {("osm:node/1", 1)}
    assert report["outside_region"] == {"places": 1, "stations": 1}
    assert report["region"] == ["r/1"]


def test_refresh_places_rewrites_only_d34_tables_and_keeps_layers(tmp_path):
    """Пересборка мест готового пакета: меняются места, станции и манифест; слой калькулятора,
    его источник и прочие файлы — нет; повтор на тех же входах даёт те же байты."""
    from datetime import UTC, datetime

    from core.packs import Manifest, PackFile, PackSource, read_manifest, sha256_file, verify
    from core.packs import write_jsonl_gz as write
    from pipeline.refresh_places import refresh
    from tests.test_seva_geo import MANIFEST, node, write_rows

    osm = tmp_path / "osm"
    osm.mkdir()
    write_rows(
        osm / "records.jsonl.gz",
        [
            _area(1, _square(39.7, 43.58, 100, 300), {"leisure": "park", "name": "Парк"}),
            node(2, 39.701, 43.58, {"railway": "station", "name": "Станция"}),
        ],
    )
    (osm / "manifest.json").write_text(
        json.dumps(MANIFEST | {"city_id": "sochi", "algorithm_version": "osm-places.1"})
    )
    pack = tmp_path / "sochi"
    (pack / "data").mkdir(parents=True)
    rows = {
        "hotels": [{"id": "h", "city_id": "sochi", "latitude": 43.58, "longitude": 39.7}],
        "places": [{"id": "osm:node/9", "city_id": "sochi", "categories": ["L01"]}],
        "transit_stations": [],
        "hotel_stations": [],
        "transit_times": [],
        "facts": [{"fact_id": "layer"}],
    }
    files = []
    for kind, records in rows.items():
        path = pack / "data" / ("view_sun" if kind == "facts" else kind)
        path = path.with_suffix(".jsonl.gz")
        write(path, records)
        files.append(
            PackFile(
                path=f"data/{path.name}",
                kind=kind,
                rows=len(records),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
                layer=True if kind == "facts" else None,
            )
        )
    layer = PackSource(id="view_sun:x", provider="test", layer="data/view_sun.jsonl.gz")
    (pack / "manifest.json").write_text(
        Manifest(
            city_id="sochi",
            city_name="Большой Сочи",
            timezone="Europe/Moscow",
            depth="full",
            pack_version="build.1-aaaa+bbbb",
            built_at=datetime(2026, 10, 27, tzinfo=UTC),
            code_version="build.1",
            sources=[layer],
            files=files,
        ).model_dump_json(exclude_none=True)
    )
    built = datetime(2026, 10, 28, 3, tzinfo=UTC)
    report = refresh(pack, osm, built_at=built)
    manifest = verify(pack)
    by_kind = {f.kind: f for f in manifest.files}
    assert by_kind["places"].rows == report["places"]["after"] > 1  # парк и его точки края
    assert report["places"]["removed"] == 1  # прежняя аптека не из этого среза
    assert by_kind["transit_stations"].rows == 1 and by_kind["hotel_stations"].rows == 1
    assert by_kind["facts"] == files[-1]  # слой не тронут
    base, layers = manifest.pack_version.split("+")  # версия сборки и хеш слоёв
    assert base.startswith("build.1-") and base != "build.1-aaaa" and layers
    assert manifest.code_version == "build.1" and manifest.built_at == built
    ids = [s.id for s in manifest.sources]
    assert ids == ["view_sun:x", "d34_places_osm"]
    assert manifest.sources[1].sha256 == MANIFEST["source_sha256"]
    first = {p.name: p.read_bytes() for p in (pack / "data").iterdir()}
    first["manifest.json"] = (pack / "manifest.json").read_bytes()
    refresh(pack, osm, built_at=built)
    again = {p.name: p.read_bytes() for p in (pack / "data").iterdir()}
    again["manifest.json"] = (pack / "manifest.json").read_bytes()
    assert again == first and read_manifest(pack).notes.count("places-refresh.1") == 1


def test_moscow_shards_share_full_graph_and_restore_route_callback(tmp_path):
    from calculators.route_geo import RoutePoiCalculator
    from calculators.route_graph import PedestrianGraph
    from core.plugins import CityContext, DatasetRef, Subject
    from tests.test_seva_geo import MANIFEST, node, way, write_rows

    manifest = MANIFEST | {"bbox": [37, 55, 38, 56]}
    rows = [
        way(1, [[1, 37.6, 55.7], [2, 37.601, 55.7], [3, 37.602, 55.7]], {"highway": "footway"}),
        node(4, 37.602, 55.7, {"highway": "bus_stop"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", rows)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    graph = PedestrianGraph("moscow", copy.deepcopy(rows), manifest, compact=True)
    subjects = [
        Subject(str(i), "hotel", f"hotel:{i}", 55.7, x) for i, x in enumerate((37.6, 37.601))
    ]
    ctx = CityContext(
        "moscow",
        "Europe/Moscow",
        tmp_path,
        {"osm_city": DatasetRef("osm_city", tmp_path, "OSM")},
        {"shared_graph": graph},
    )
    calculator = RoutePoiCalculator()
    combined = calculator.run(subjects, ctx)
    expected = {f.fact_id: f.model_dump(mode="json") for f in combined}
    split = [f for s in subjects for f in calculator.run([s], ctx)]
    assert expected == {f.fact_id: f.model_dump(mode="json") for f in split}
    assert graph.route.__name__ == "route"
    routes = calculator.outputs["moscow_all_selected_routes.jsonl"]
    assert any(3 in r["properties"]["node_ids"] for r in routes)


def test_layers_reject_orphan_references(tmp_path):
    from pipeline.build_packs import validate_parts

    parts = {
        "hotels": [{"id": "h"}],
        "facts": [],
        "hotel_stations": [{"hotel_id": "h", "station_id": "missing", "distance_m": 1}],
    }
    with pytest.raises(ValueError, match="ссылка"):
        validate_parts(parts)
    parts = {"hotels": [{"id": "h"}], "facts": [], "places": [{"id": "p", "categories": []}]}
    with pytest.raises(ValueError, match="без категории"):
        validate_parts(parts)


def test_building_review_covers_unmatched_hotels_without_accepting_v11_claims(tmp_path):
    from core.packs import sha256_file
    from pipeline.building_review import import_v11, items
    from tests.test_seva_geo import write_rows

    hotels = [
        {"id": "a", "city_id": "moscow", "address": "1Б"},
        {"id": "b", "city_id": "moscow", "address": "1"},
    ]
    queue = items(hotels, [], {"review_candidates": []}, "a" * 64)
    assert len(queue) == 2 and all(
        r["state"] == "proposed" and r["proposal"]["building_id"] is None for r in queue
    )
    assert queue == items(list(reversed(hotels)), [], {"review_candidates": []}, "a" * 64)
    row = queue[0] | {"state": "accepted", "reviewer": "claimed-by-external-file"}
    path = tmp_path / "v11.jsonl.gz"
    write_rows(path, [row])
    with pytest.raises(ValueError, match="SHA256"):
        import_v11(path, "wrong", {"a", "b"}, "moscow")
    imported = import_v11(path, sha256_file(path), {"a", "b"}, "moscow")
    assert imported[0]["state"] == "proposed" and "reviewer" not in imported[0]


def test_twogis_bundle_is_dated_separate_and_keeps_access_unknown(tmp_path):
    from datetime import UTC, datetime

    from pipeline.build_packs import write_pack
    from pipeline.collect_twogis import collect
    from pipeline.twogis_layers import merge

    source = {"provider": "2gis", "fetched_at": "2026-10-26T12:00:00Z", "commercial_ok": False}
    place = {
        "id": "2gis:1",
        "city_id": "moscow",
        "name": "Italian",
        "latitude": 55.7,
        "longitude": 37.6,
        "categories": ["italian"],
        "tags": {},
        "source": source,
    }
    path = tmp_path / "twogis.json"
    bundle = {
        "format": "twogis-layers.1",
        "city_id": "moscow",
        "commercial_ok": False,
        "places": [place],
        "transit_times": [],
    }
    path.write_text(json.dumps(bundle))
    parts = {
        "hotels": [
            {"id": "h", "city_id": "moscow", "name": "Hotel", "latitude": 55.7, "longitude": 37.6}
        ],
        "facts": [],
        "places": [],
        "transit_times": [],
    }
    passport = merge(parts, path, "moscow")
    assert parts["places"] == [place] and "hotel_places" not in parts
    manifest = write_pack(
        tmp_path / "out",
        "moscow",
        parts,
        sources=[passport],
        built_at=datetime(2026, 10, 26, tzinfo=UTC),
        code_version="test",
        notes="test",
    )
    assert any(f.path.endswith("places_2gis.jsonl.gz") for f in manifest.files)
    # Факт с паспортом 2ГИС в `sources` — тоже в отдельный файл (D37).
    gis_fact = {
        "fact_id": "f2gis",
        "hotel_id": "h",
        "city_id": "moscow",
        "subject_level": "hotel",
        "subject_id": "hotel:h",
        "feature_id": "L01",
        "status": "unknown",
        "unknown_reason": "test",
        "sources": [source],
    }
    facts = write_pack(
        tmp_path / "out-facts",
        "moscow",
        parts | {"facts": [gis_fact]},
        sources=[passport],
        built_at=datetime(2026, 10, 26, tzinfo=UTC),
        code_version="test",
        notes="test",
    )
    assert [f.path for f in facts.files if f.kind == "facts"] == ["data/facts_2gis.jsonl.gz"]
    assert manifest.sources[0].commercial_ok is False
    mapping = tmp_path / "rubrics.json"
    mapping.write_text(
        json.dumps({"region_id": 32, "rubric_categories": {"verified-id": ["italian"]}})
    )
    plan = collect(tmp_path / "out/moscow", mapping, tmp_path / "cache", tmp_path / "not-created")
    assert plan["live_requests"] is False and not (tmp_path / "not-created").exists()
    bundle["places"][0]["tags"]["photos"] = ["forbidden"]
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match="forbidden"):
        merge(parts, path, "moscow")


def test_saved_route_ascent_matches_fresh_full_calculation(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from calculators.route_geo import RoutePoiCalculator
    from core.plugins import CityContext, DatasetRef, Subject
    from pipeline.complete_ascent import complete
    from tests.test_seva_geo import MANIFEST, node, way, write_rows

    manifest = MANIFEST | {"bbox": [37, 55, 38, 56]}
    rows = [
        way(1, [[1, 37.6, 55.7], [2, 37.601, 55.7], [3, 37.602, 55.7]], {"highway": "footway"}),
        node(4, 37.602, 55.7, {"highway": "bus_stop"}),
    ]
    write_rows(tmp_path / "records.jsonl.gz", rows)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    rasters = tmp_path / "rasters"
    rasters.mkdir()
    tile = rasters / "Copernicus_DSM_test.tif"
    data = np.tile(np.arange(100, dtype="float32"), (100, 1))
    with rasterio.open(
        tile,
        "w",
        driver="GTiff",
        width=100,
        height=100,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(37.599, 55.705, 0.0001, 0.0001),
    ) as dataset:
        dataset.write(data, 1)
    subject = Subject("h", "hotel", "hotel:h", 55.7, 37.6)
    ctx = CityContext(
        "moscow", "Europe/Moscow", tmp_path, {"osm_city": DatasetRef("osm_city", tmp_path, "OSM")}
    )
    calc = RoutePoiCalculator()
    unknowns = [f.model_dump(mode="json", exclude_none=True) for f in calc.run([subject], ctx)]
    routes = {
        r["properties"]["route_id"]: r["geometry"]["coordinates"]
        for r in calc.outputs["moscow_all_selected_routes.jsonl"]
        if r["geometry"]["type"] == "LineString"
    }
    parts = {"facts": unknowns}
    report = complete(parts, routes, rasters, manifest, "moscow")
    assert report["updated_facts"] == 2 and not report["physical_ground_profile_verified"]
    fresh_ctx = CityContext(
        "moscow", "Europe/Moscow", tmp_path, ctx.datasets, {"raster_dir": str(rasters)}
    )
    fresh = [
        f.model_dump(mode="json", exclude_none=True)
        for f in calc.run([subject], fresh_ctx)
        if f.feature_id == "W04"
    ]
    patched = [f for f in parts["facts"] if f["feature_id"] == "W04"]
    assert fresh == patched
    assert all(
        f["value"]["dsm_positive_dz_estimate_m"] > 0 and f["hard_condition_eligible"] is False
        for f in patched
    )
