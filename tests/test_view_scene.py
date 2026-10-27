"""Сцена высот: чистые функции, без сети.

Проверяем то, что легко сломать молча: высота из `height`/`building:levels`, расстояние и азимут,
площадь контура, «своё здание» (сопоставление v9 и точка в контуре, а не название) и чтение
выгрузки OSM вместе с зданиями-отношениями. Сетевого пути в модуле нет: API редактирования OSM
для массового чтения запрещён политикой OSMF (ревью №22).
"""

from __future__ import annotations

import json

import pytest

import calculators.view_scene as scene

HOTEL = {
    "hotel_id": "1",
    "name": "LAVICON Отель Фрегат, Адлер",
    "latitude": 43.5,
    "longitude": 39.75,
}
SOURCE = {"provider": "OpenStreetMap", "license": "ODbL 1.0"}


def square(lat: float, lon: float, half: float = 0.0001) -> list[tuple[float, float]]:
    """Замкнутый квадрат вокруг точки, (lat, lon)."""
    ring = [
        (lat - half, lon - half),
        (lat - half, lon + half),
        (lat + half, lon + half),
        (lat + half, lon - half),
    ]
    return ring + ring[:1]


def element(osm_id: str, ring, **tags) -> dict:
    return {
        "osm_id": osm_id,
        "tags": {"building": "yes", **tags},
        "polygons": [{"outer": ring, "inner": []}],
    }


def test_height_from_height_tag():
    assert scene.height_of({"height": "27"}) == (27.0, "height")
    assert scene.height_of({"height": "27 m"}) == (27.0, "height")
    assert scene.height_of({"height": "27,5"}) == (27.5, "height")


def test_height_from_levels_when_height_absent():
    height, source = scene.height_of({"building:levels": "9"})
    assert height == 27.0 and source == "levels_x3m"


def test_height_missing_is_not_invented():
    assert scene.height_of({"building": "yes"}) == (None, "missing")
    assert scene.height_of({"height": "unknown", "building:levels": "n/a"}) == (None, "missing")


def test_distance_and_bearing():
    # 0.001° по широте ≈ 111 м строго на север
    assert round(scene.haversine_m(43.5, 39.75, 43.501, 39.75)) == 111
    assert round(scene.bearing_deg(43.5, 39.75, 43.501, 39.75)) == 0
    assert round(scene.bearing_deg(43.5, 39.75, 43.5, 39.751)) == 90


def test_footprint_area_of_square():
    # квадрат ~100×100 м вокруг точки
    dlat = 100 / 111_320
    dlon = 100 / (111_320 * 0.725)
    sq = [
        (43.5 - dlat / 2, 39.75 - dlon / 2),
        (43.5 - dlat / 2, 39.75 + dlon / 2),
        (43.5 + dlat / 2, 39.75 + dlon / 2),
        (43.5 + dlat / 2, 39.75 - dlon / 2),
    ]
    assert 9_000 < scene.polygon_area_m2(sq) < 11_000


def test_name_match_is_only_a_hint_without_generic_words():
    assert scene.name_match("Radisson Collection Парадиз СПА, Сочи", "Radisson Collection")
    assert scene.name_match("LAVICON Отель Фрегат, Адлер", "LAVICON HOTEL FREGAT")
    # Ревью №22: «отель», «Сочи», «Адлер» есть почти в любом названии — это соседи, не корпус
    assert not scene.name_match("LAVICON Отель Фрегат, Адлер", "Отель Нептун")
    assert not scene.name_match("Radisson Collection Парадиз СПА, Сочи", "Гранд Отель Сочи")
    assert not scene.name_match("Отель Ромашка", "Жилой дом на Ленина")


def test_own_building_by_v9_match_or_point_not_by_name():
    """Ревью №22: корпуса Radisson без имени шли препятствиями самому отелю, а здание с
    «Отель» в имени по названию выпало бы из препятствий."""
    elements = [
        element("way/1", square(43.5, 39.75)),  # точка отеля внутри контура, имени нет
        element("way/2", square(43.5004, 39.7504)),  # корпус из сопоставления v9, имени нет
        element("way/3", square(43.5008, 39.75), name="Отель Нептун"),  # сосед
        element("way/4", square(43.5003, 39.7495), name="LAVICON HOTEL FREGAT"),  # только имя
    ]
    built = scene.build_scene(HOTEL, elements, 200, SOURCE, frozenset({"way/2"}))
    by_id = {o["osm_id"]: o for o in built["obstacles"]}
    assert by_id["way/1"]["self_building"] and by_id["way/1"]["self_source"] == "contains_point"
    assert by_id["way/2"]["self_building"] and by_id["way/2"]["self_source"] == "v9_match"
    assert not by_id["way/3"]["self_building"] and not by_id["way/3"]["name_match"]
    assert not by_id["way/4"]["self_building"] and by_id["way/4"]["name_match"]
    assert built["counts"]["self_buildings"] == 2
    # ближайшее — чужое здание: свои корпуса себе не препятствие
    assert built["counts"]["nearest_m"] == by_id["way/4"]["distance_m"]


def test_point_in_hole_is_not_inside():
    outer = square(43.5, 39.75, half=0.001)
    hole = square(43.5, 39.75, half=0.0002)
    assert scene.contains_point([{"outer": outer, "inner": []}], 43.5, 39.75)
    assert not scene.contains_point([{"outer": outer, "inner": [hole]}], 43.5, 39.75)


def test_scene_filters_by_radius_and_sorts():
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 10,
                "tags": {"building": "yes", "building:levels": "5", "name": "Дальний"},
                "geometry": [
                    {"lat": 43.5012, "lon": 39.75},
                    {"lat": 43.5012, "lon": 39.7505},
                    {"lat": 43.5016, "lon": 39.7505},
                ],
            },
            {
                "type": "way",
                "id": 11,
                "tags": {"building": "yes", "building:levels": "2"},
                "geometry": [
                    {"lat": 43.5005, "lon": 39.75},
                    {"lat": 43.5005, "lon": 39.7505},
                    {"lat": 43.501, "lon": 39.7505},
                ],
            },
        ],
        "_fetched_at": "2026-10-26T00:00:00+00:00",
        "_query": "test",
    }
    elements = scene.cache_elements(payload)
    built = scene.build_scene(HOTEL, elements, 200, SOURCE)
    assert built["counts"]["buildings"] == 2
    assert [o["osm_id"] for o in built["obstacles"]] == ["way/11", "way/10"]  # ближний первый
    assert built["obstacles"][1]["height_m"] == 15.0
    assert built["scene_version"] == scene.SCENE_VERSION

    narrow = scene.build_scene(HOTEL, elements, 100, SOURCE)
    assert narrow["counts"]["buildings"] == 1


def test_centre_does_not_depend_on_ring_start():
    ring = square(43.5005, 39.7505)
    shifted = ring[2:-1] + ring[:3]  # тот же контур, начат с другой вершины
    a = scene.build_scene(HOTEL, [element("way/1", ring)], 200, SOURCE)["obstacles"][0]
    b = scene.build_scene(HOTEL, [element("way/1", shifted)], 200, SOURCE)["obstacles"][0]
    assert (a["distance_m"], a["azimuth_deg"]) == (b["distance_m"], b["azimuth_deg"])


def test_no_network_path():
    assert not hasattr(scene, "fetch_osm")
    assert "urllib" not in vars(scene)


def test_extract_reads_ways_and_relations(tmp_path):
    shapely = pytest.importorskip("shapely")

    def row(osm_type, osm_id, geom, **tags):
        return {
            "osm_type": osm_type,
            "osm_id": osm_id,
            "geometry_wkb": shapely.to_wkb(geom, hex=True),
            "tags": tags,
            "version": 1,
        }

    def poly(lat, lon, half=0.0001):
        return shapely.Polygon([(y, x) for x, y in square(lat, lon, half)])

    rows = [
        row("way", 1, shapely.MultiPolygon([poly(43.5004, 39.7504)]), building="yes"),
        row(
            "relation",
            2,
            shapely.MultiPolygon([poly(43.5, 39.751), poly(43.5, 39.7512)]),
            building="apartments",
            **{"building:levels": "4"},
        ),  # fmt: skip
        row("node", 3, shapely.Point(39.75, 43.5), building="yes"),  # точка — не контур
        row("way", 4, shapely.MultiPolygon([poly(43.6, 39.9)]), building="yes"),  # далеко
        row("way", 5, shapely.MultiPolygon([poly(43.5, 39.7503)]), highway="footway"),
    ]
    path = tmp_path / "osm_sochi_extended.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    elements = scene.extract_elements(path, [scene.box(43.5, 39.75, 200)])
    assert [e["osm_id"] for e in elements] == ["way/1", "relation/2"]
    built = scene.build_scene(HOTEL, elements, 200, SOURCE)
    relation = next(o for o in built["obstacles"] if o["osm_id"] == "relation/2")
    assert relation["height_m"] == 12.0 and relation["footprint_m2"] > 0
