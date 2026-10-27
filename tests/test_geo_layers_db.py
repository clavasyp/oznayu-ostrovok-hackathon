"""Новые слои: настоящая PostGIS, COPY, места рядом запросом, API, новая версия и откат."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="нужна PostGIS")


def test_geo_pack_roundtrip_api_replace_and_copy_rollback(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from psycopg.errors import UniqueViolation
    from sqlalchemy import create_engine, func, insert, select
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import IntegrityError

    import pipeline.load_packs as loader
    from app.main import create_app
    from core.db import CurationItem, Facade, FactRow, HotelStation, PackLoad
    from core.packs import Manifest, PackFile, sha256_file
    from pipeline.build_v9_packs import write_jsonl_gz
    from pipeline.load_packs import load_pack

    monkeypatch.setattr(loader, "BULK_FK_THRESHOLD", 0)

    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_MODE", "db")
    engine = create_engine(url)
    command.upgrade(Config("alembic.ini"), "head")
    city = "geo_test_" + uuid4().hex
    # Точки в Гвинейском заливе: места рядом считает запрос по всей таблице `place`, а в базе
    # могут лежать и настоящие пакеты.
    hid, bid, sid = city + "hotel", city + "building", city + "station"
    parts = {
        "hotels": [
            {"id": hid, "city_id": city, "name": "Test hotel", "latitude": 0.7, "longitude": 0.6}
        ],
        "buildings": [
            {
                "id": bid,
                "city_id": city,
                "tags": {"name": "Mapped name"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0.6, 0.7], [0.601, 0.7], [0.601, 0.701], [0.6, 0.7]]],
                },
            }
        ],
        "hotel_buildings": [
            {"hotel_id": hid, "building_id": bid, "method": "auto", "evidence": {"rule": "test"}}
        ],
        "facades": [
            {
                "building_id": bid,
                "facade_id": "west",
                "azimuth_deg": 270,
                "side": "W",
                "is_courtyard": None,
                "length_m": 50,
                "source": {"provider": "OpenStreetMap"},
                "geometry": {"type": "LineString", "coordinates": [[0.6, 0.7], [0.6, 0.701]]},
            }
        ],
        "places": [
            {
                "id": city + "place",
                "city_id": city,
                "name": "Italian",
                "latitude": 0.7001125,  # ≈12,4 м к северу от отеля
                "longitude": 0.6,
                "categories": ["L02", "italian"],
                "tags": {"amenity": "restaurant", "cuisine": "italian"},
                "source": {"provider": "OpenStreetMap"},
            },
            {  # корпус самого отеля, нанесённый как место: в его выдачу не попадает
                "id": "osm:" + bid,
                "city_id": city,
                "name": "Test hotel cafe",
                "latitude": 0.7,
                "longitude": 0.6,
                "categories": ["L02", "coffee"],
                "tags": {"amenity": "cafe", "tourism": "hotel"},
                "source": {"provider": "OpenStreetMap"},
            },
            {  # дальше 1500 м
                "id": city + "far",
                "city_id": city,
                "name": "Far pharmacy",
                "latitude": 0.72,
                "longitude": 0.6,
                "categories": ["L01"],
                "tags": {"amenity": "pharmacy"},
                "source": {"provider": "OpenStreetMap"},
            },
        ],
        "transit_stations": [
            {
                "id": sid,
                "city_id": city,
                "name": "Station",
                "latitude": 0.7,
                "longitude": 0.6,
                "mode": "metro",
                "source": {"provider": "OpenStreetMap"},
            }
        ],
        "hotel_stations": [
            {
                "hotel_id": hid,
                "station_id": sid,
                "rank": 1,
                "distance_m": 100,
                "distance_kind": "geodesic_to_representative_point",
                "walking_distance_m": None,
                "source": {"method": "test"},
            }
        ],
        "transit_times": [],
        "curation_items": [
            {
                "id": int(city[-12:], 16),
                "city_id": city,
                "kind": "match_hotel_building",
                "subject_level": "hotel",
                "subject_id": hid,
                "feature_id": None,
                "proposal": {"building_id": bid},
                "evidence": [{"kind": "test"}],
                "state": "proposed",
                "proposed_by": "script:test",
            }
        ],
    }

    def write(version, duplicate=False, orphan=False):
        pack = tmp_path / version
        (pack / "data").mkdir(parents=True)
        files = []
        for kind, records in sorted(parts.items()):
            rows = records * 2 if duplicate and kind == "hotel_stations" else records
            if orphan and kind == "hotel_stations":
                rows = [r | {"station_id": "missing_station"} for r in rows]
            path = pack / "data" / (kind + ".jsonl.gz")
            write_jsonl_gz(path, rows)
            files.append(
                PackFile(
                    path="data/" + path.name,
                    kind=kind,
                    rows=len(rows),
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
        manifest = Manifest(
            city_id=city,
            city_name="Geo test",
            timezone="Europe/Moscow",
            depth="full",
            pack_version=version,
            built_at=datetime(2026, 10, 26, tzinfo=UTC),
            files=files,
        )
        (pack / "manifest.json").write_text(manifest.model_dump_json())
        return pack

    first = write("first")
    with engine.begin() as conn:
        assert "загружено" in load_pack(conn, first)
        assert "уже загружен" in load_pack(conn, first)
        conn.execute(
            insert(FactRow).values(
                fact_id=city + "runtime",
                hotel_id=hid,
                city_id=city,
                subject_level="hotel",
                subject_id="hotel:" + hid,
                feature_id="P01",
                status="unknown",
                unknown_reason="test",
                extra={},
            )
        )
    with TestClient(create_app(ui_dist=tmp_path / "no-ui")) as client:
        assert client.get("/health").status_code == 200
        geometry = client.get(f"/api/hotels/{hid}/geometry").json()
        assert geometry["facades"][0]["facade_id"] == "west"
        assert geometry["facades"][0]["is_courtyard"] is None
        assert geometry["placement_building_choices"][0]["building_id"] == bid
        assert geometry["placement_building_choices"][0]["hotel_aliases"] == []
        assert "корпус 2" not in geometry["placement_building_aliases"]
        places = client.get(
            f"/api/hotels/{hid}/nearby-places", params={"category": "italian"}
        ).json()
        assert places["total"] == 1 and places["status"] == "observed"
        proximity = places["places"][0]["proximity"]
        assert proximity["walking_distance_m"] is None and 12.0 < proximity["distance_m"] < 13.0
        assert proximity["distance_kind"] == "geodesic_to_representative_point"
        assert proximity["source"]["passport_ref"] == "place.source"
        assert "geog" not in places["places"][0]["place"]
        everything = client.get(f"/api/hotels/{hid}/nearby-places").json()
        assert [p["place"]["id"] for p in everything["places"]] == [city + "place"]
        pharmacies = client.get(f"/api/hotels/{hid}/nearby-places?category=L01").json()
        assert pharmacies["total"] == 0 and pharmacies["status"] == "unknown"
        wrong = client.get(f"/api/hotels/{hid}/nearby-places?category=pharmacy")
        assert wrong.status_code == 422 and "L01" in wrong.json()["detail"]
        assert client.get(f"/api/hotels/{hid}/nearby-places?radius_m=1600").status_code == 422
        assert len(client.get(f"/api/hotels/{hid}/stations").json()["stations"]) == 1
        params = {
            "source_station_id": sid,
            "target_station_id": sid,
            "departure_at": "2026-11-14T02:00:00+03:00",
        }
        answer = client.get(f"/api/cities/{city}/transit-times", params=params).json()
        assert answer["status"] == "unknown" and answer["routes"] == []
        assert client.get("/api/cities/nowhere/transit-times", params=params).status_code == 404
        params["departure_at"] = "2026-11-14T02:00:00"
        assert client.get(f"/api/cities/{city}/transit-times", params=params).status_code == 422
    with engine.begin() as conn:
        conn.execute(
            CurationItem.__table__.update()
            .where(CurationItem.id == parts["curation_items"][0]["id"])
            .values(state="unsure", reviewer="human")
        )
        load_pack(conn, write("second"))
    # Повтор ключа ловит сборка первичного ключа после COPY; потерянную ссылку — VALIDATE FK.
    for broken, sqlstate in (("broken", "23505"), ("orphan", "23503")):
        with pytest.raises((UniqueViolation, IntegrityError)) as error, engine.begin() as conn:
            load_pack(conn, write(broken, duplicate=broken == "broken", orphan=broken == "orphan"))
        assert getattr(error.value, "orig", error.value).sqlstate == sqlstate
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count()).select_from(HotelStation).where(HotelStation.hotel_id == hid)
            )
            == 1
        )
        assert (
            conn.scalar(select(func.count()).select_from(Facade).where(Facade.building_id == bid))
            == 1
        )
        assert (
            conn.scalar(select(PackLoad.pack_version).where(PackLoad.city_id == city)) == "second"
        )
        assert conn.scalar(select(FactRow.fact_id).where(FactRow.fact_id == city + "runtime"))
        assert (
            conn.scalar(
                select(CurationItem.state).where(
                    CurationItem.id == parts["curation_items"][0]["id"]
                )
            )
            == "unsure"
        )
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='hotel_station'::regclass "
                "AND contype IN ('f', 'p') AND convalidated"
            ).scalar_one()
            == 3
        )
        assert conn.exec_driver_sql("SELECT to_regclass('public.hotel_place')").scalar() is None
        # Индексы, снятые на время COPY, восстановлены ровно такими же.
        assert set(
            conn.exec_driver_sql(
                "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                "AND tablename IN ('fact', 'place', 'hotel_station')"
            ).scalars()
        ) >= {
            "fact_pkey",
            "ix_fact_hotel_id",
            "ix_fact_hotel_feature",
            "ix_fact_city_feature_status",
            "place_pkey",
            "ix_place_geog",
            "hotel_station_pkey",
            "ix_hotel_station_station_id",
        }
    engine.dispose()
