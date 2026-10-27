"""Поставляемые пакеты (`packs/`) в настоящей PostGIS — ровно то, что грузит `init` у жюри.

Остальные тесты на базе грузят v9 из временной папки или синтетические пакеты; этот —
настоящие Москву и Сочи (полный каталог Островка). Медленно, несколько минут, поэтому только
с TEST_DATABASE_URL и SHIPPED_PACKS=1: `make test-packs`. Запускать после каждой пересборки
пакетов и перед тегом submission. Тест очищает базу — только `*_test`.
"""

import os
import time
from pathlib import Path

import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_URL or os.environ.get("SHIPPED_PACKS") != "1",
    reason="поставляемые пакеты: TEST_DATABASE_URL и SHIPPED_PACKS=1 (make test-packs)",
)

ROOT = Path(__file__).resolve().parents[1]
RADISSON = "13736470"  # Сочи, сценарии показа
MOSCOW = ("8736052", "6458292")  # «Прованс», Bolshaya Dorogomilovskaya
# Сколько строк каждого вида пакета лежит в базе у города.
COUNTS = {
    "hotels": "SELECT count(*) FROM hotel WHERE city_id = :c",
    "room_types": "SELECT count(*) FROM room_type r JOIN hotel h ON h.id = r.hotel_id "
    "WHERE h.city_id = :c",
    "placement_sets": "SELECT count(*) FROM placement_set p JOIN room_type r "
    "ON r.id = p.room_type_id JOIN hotel h ON h.id = r.hotel_id WHERE h.city_id = :c",
    "placements": "SELECT count(*) FROM placement p JOIN room_type r ON r.id = p.room_type_id "
    "JOIN hotel h ON h.id = r.hotel_id WHERE h.city_id = :c",
    "placement_windows": "SELECT count(*) FROM placement_window w JOIN placement p "
    "ON p.id = w.placement_id JOIN room_type r ON r.id = p.room_type_id "
    "JOIN hotel h ON h.id = r.hotel_id WHERE h.city_id = :c",
    "buildings": "SELECT count(*) FROM building WHERE city_id = :c",
    "hotel_buildings": "SELECT count(*) FROM hotel_building b JOIN hotel h ON h.id = b.hotel_id "
    "WHERE h.city_id = :c",
    "facades": "SELECT count(*) FROM facade f JOIN building b ON b.id = f.building_id "
    "WHERE b.city_id = :c",
    "places": "SELECT count(*) FROM public.place WHERE city_id = :c",
    "transit_stations": "SELECT count(*) FROM transit_station WHERE city_id = :c",
    "hotel_stations": "SELECT count(*) FROM hotel_station s JOIN hotel h ON h.id = s.hotel_id "
    "WHERE h.city_id = :c",
    "transit_times": "SELECT count(*) FROM transit_time WHERE city_id = :c",
    "curation_items": "SELECT count(*) FROM curation_item WHERE city_id = :c",
    "facts": "SELECT count(*) FROM fact WHERE city_id = :c AND extra ? 'pack'",
    "questions": "SELECT count(*) FROM question q JOIN hotel h ON h.id = q.hotel_id "
    "WHERE h.city_id = :c",
    "coverage": "SELECT count(*) FROM city_coverage WHERE city_id = :c",
    "ota_comparisons": "SELECT count(*) FROM ota_comparison WHERE city_id = :c",
}


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from pipeline import init

    assert make_url(TEST_URL).database.endswith("_test"), "тест очищает базу — только *_test"
    eng = create_engine(TEST_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    saved = {name: os.environ.get(name) for name in ("DATABASE_URL", "PACKS_DIR")}
    os.environ.update(DATABASE_URL=TEST_URL, PACKS_DIR=str(ROOT / "packs"))
    started = time.monotonic()
    try:
        assert init.main() == 0  # тот же путь, что `python -m pipeline.init` в compose
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    print(f"\nshipped packs: init {time.monotonic() - started:.1f} с")
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def client(engine, tmp_path_factory):
    from fastapi.testclient import TestClient

    from app.main import create_app

    saved = {name: os.environ.get(name) for name in ("DATA_MODE", "DATABASE_URL")}
    os.environ.update(DATA_MODE="db", DATABASE_URL=TEST_URL)
    try:
        with TestClient(create_app(ui_dist=tmp_path_factory.mktemp("no-ui"))) as c:
            yield c
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_rows_in_database_match_manifests(engine):
    from sqlalchemy import text

    from core.packs import discover, read_manifest, sha256_file

    packs = discover(ROOT / "packs")
    assert {p.name for p in packs} >= {"moscow", "sochi"}
    with engine.connect() as conn:
        for pack in packs:
            manifest = read_manifest(pack)
            expected = {}
            for entry in manifest.files:
                expected[entry.kind] = expected.get(entry.kind, 0) + entry.rows
            for kind, rows in expected.items():
                found = conn.execute(text(COUNTS[kind]), {"c": manifest.city_id}).scalar_one()
                assert found == rows, f"{manifest.city_id}: {kind} {found} ≠ {rows}"
            loaded = conn.execute(
                text("SELECT pack_version, manifest_sha256 FROM pack_load WHERE city_id = :c"),
                {"c": manifest.city_id},
            ).one()
            assert tuple(loaded) == (manifest.pack_version, sha256_file(pack / "manifest.json"))


def test_health_cities_and_coverage(client):
    from core.packs import read_manifest

    assert client.get("/health").json() == {"status": "ok"}
    cities = {c["city_id"]: c for c in client.get("/api/cities").json()["cities"]}
    for city in ("moscow", "sochi"):
        manifest = read_manifest(ROOT / "packs" / city)
        hotels = sum(f.rows for f in manifest.files if f.kind == "hotels")
        assert cities[city]["depth"] == "full" and cities[city]["hotels"] == hotels
        assert len(client.get(f"/api/cities/{city}/coverage").json()["features"]) == 54


def test_sochi_subzones_are_names_with_codes(client):
    from connectors.city_geography import SUBZONE_NAMES

    hotels = client.get("/api/hotels", params={"city_id": "sochi"}).json()["hotels"]
    assert hotels and all(h["subzone"] == SUBZONE_NAMES[h["subzone_id"]] for h in hotels)


def test_showcase_hotels_card_questions_geometry_and_places(client):
    radisson = client.get(f"/api/hotels/{RADISSON}").json()
    assert "Radisson" in radisson["hotel"]["name"] and radisson["facts"]
    assert radisson["room_types"] and radisson["hotel"]["subzone_id"]
    assert client.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
    geometry = client.get(f"/api/hotels/{RADISSON}/geometry").json()
    own = {"osm:" + link["building_id"] for link in geometry["hotel_buildings"]}
    assert geometry["buildings"] and own
    nearby = client.get(f"/api/hotels/{RADISSON}/nearby-places", params={"limit": 500}).json()
    distances = [p["proximity"]["distance_m"] for p in nearby["places"]]
    assert nearby["status"] == "observed" and distances == sorted(distances)
    assert distances[-1] <= 1500 and not own & {p["place"]["id"] for p in nearby["places"]}
    assert all(p["place"]["categories"] for p in nearby["places"])  # скамеек и урн нет
    assert client.get(f"/api/hotels/{RADISSON}/nearby-places?category=pool").json()["total"]
    assert client.get(f"/api/hotels/{RADISSON}/nearby-places?category=x").status_code == 422
    assert client.get(f"/api/hotels/{RADISSON}/stations").json()["stations"]
    for hotel_id in MOSCOW:
        card = client.get(f"/api/hotels/{hotel_id}").json()
        assert card["hotel"]["city_id"] == "moscow" and card["facts"]
