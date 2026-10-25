"""Интеграция с настоящим PostGIS: миграции, загрузка пакетов, хранилище на базе, API в режиме db.

Нужна отдельная тестовая база, имя которой кончается на `_test` (тест её очищает):
`make test-db` поднимает базу и запускает эти тесты. Без TEST_DATABASE_URL тесты пропускаются.
"""

import gzip
import json
import os
import shutil
from pathlib import Path

import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="нужна тестовая база: make test-db")

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"
CITIES = ("sochi", "moscow")


@pytest.fixture(scope="module")
def engine():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from pipeline.load_packs import load_pack

    assert make_url(TEST_URL).database.endswith("_test"), "тест очищает базу — только *_test"
    eng = create_engine(TEST_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_URL  # migrations/env.py берёт адрес отсюда
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if saved is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = saved
    for city in CITIES:
        with eng.begin() as conn:
            load_pack(conn, PACKS / city)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def stores(engine):
    from app.stub_store import StubStore
    from core.store_db import DbStore

    return DbStore(engine), StubStore.load()


def test_same_pack_is_not_loaded_twice(engine):
    from pipeline.load_packs import load_pack

    with engine.begin() as conn:
        assert "уже загружен" in load_pack(conn, PACKS / "sochi")


def test_db_facts_equal_fixture_facts(stores):
    db, stub = stores
    hotels = [h["hotel_id"] for h in stub.hotels() if h["city_id"] in CITIES]
    assert hotels
    for hotel_id in hotels:
        a = {
            f.fact_id: f.model_dump(mode="json")
            for fs in db.hotel_facts(hotel_id).values()
            for f in fs
        }
        b = {
            f.fact_id: f.model_dump(mode="json")
            for fs in stub.hotel_facts(hotel_id).values()
            for f in fs
        }
        assert a == b, hotel_id


def test_search_on_db_matches_fixtures(stores):
    from core.search import example_ast, search

    db, stub = stores
    ast = example_ast()
    on_db = {r["hotel"]["hotel_id"]: r for r in search(db, ast)}
    on_stub = {r["hotel"]["hotel_id"]: r for r in search(stub, ast)}
    assert len(on_db) == len(db.hotels(ast.city_id))  # весь город, а не только отели показа
    for hotel_id, r in on_stub.items():
        assert on_db[hotel_id]["status"] == r["status"], hotel_id
        assert [h["verdict"] for h in on_db[hotel_id]["hard"]] == [h["verdict"] for h in r["hard"]]


def test_api_in_db_mode(engine, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("DATA_MODE", "db")
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    with TestClient(create_app(ui_dist=tmp_path)) as c:
        assert c.get("/health").json() == {"status": "ok"}
        cities = {x["city_id"]: x for x in c.get("/api/cities").json()["cities"]}
        assert cities["sochi"]["depth"] == "full" and cities["sochi"]["hotels"] == 89
        assert len(c.get("/api/cities/sochi/coverage").json()["features"]) == 54
        body = c.post("/api/search", json={"city_id": "sochi"}).json()
        assert body["stub"] is False and len(body["results"]) == 89
        hotel = c.get("/api/hotels/13736470").json()
        assert hotel["hotel"]["name"] and hotel["facts"]
        assert c.get("/api/hotelier/13736470/questions").json()["questions"]


def test_new_pack_version_replaces_city_but_keeps_runtime_facts(engine, tmp_path):
    from sqlalchemy import func, insert, select

    from core.db import FactRow
    from core.packs import Manifest, sha256_file
    from pipeline.load_packs import load_pack

    def count(**where):
        with engine.connect() as conn:
            q = select(func.count()).select_from(FactRow).filter_by(**where)
            return conn.execute(q).scalar_one()

    # Факт «из работы» (не из пакета): новая версия пакета его не трогает.
    with engine.begin() as conn:
        conn.execute(
            insert(FactRow).values(
                fact_id="runtime-1",
                hotel_id="8736052",
                city_id="moscow",
                subject_level="hotel",
                subject_id="hotel:8736052",
                feature_id="S04",
                value={"answer": "да"},
                status="hotelier",
                extra={},
            )
        )
    before = count(city_id="moscow")

    pack = tmp_path / "moscow"
    shutil.copytree(PACKS / "moscow", pack)
    facts_path = pack / "data" / "facts.jsonl.gz"
    with gzip.open(facts_path, "rt", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f][:100]
    with gzip.open(facts_path, "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    manifest = Manifest.model_validate_json((pack / "manifest.json").read_text("utf-8"))
    for file in manifest.files:
        if file.kind == "facts":
            file.rows, file.bytes, file.sha256 = (
                len(rows),
                facts_path.stat().st_size,
                sha256_file(facts_path),
            )
    manifest.pack_version += "-test"
    (pack / "manifest.json").write_text(manifest.model_dump_json(), "utf-8")

    with engine.begin() as conn:
        load_pack(conn, pack)
    assert count(city_id="moscow") == 101  # 100 из новой версии + факт из работы
    assert before > 101

    with engine.begin() as conn:  # вернуть настоящий пакет
        load_pack(conn, PACKS / "moscow")
    assert count(city_id="moscow") == before


def test_placements_load_and_drive_search(engine, tmp_path):
    """Пакет с категориями и размещениями (D17) → база → хранилище → поиск."""
    from datetime import UTC, datetime

    from agents.query_ast import Condition, QueryAST
    from core.facts import make_fact_id
    from core.packs import Manifest, PackFile, sha256_file
    from core.search import search, window_subject
    from core.store_db import DbStore
    from pipeline.build_v9_packs import write_jsonl_gz
    from pipeline.load_packs import load_pack

    def fact(rt_or_window, level, feature, value):
        return {
            "fact_id": make_fact_id("t1", rt_or_window, feature, {}, "test"),
            "hotel_id": "t1",
            "city_id": "testcity",
            "subject_level": level,
            "subject_id": rt_or_window,
            "feature_id": feature,
            "value": value,
            "status": "computed",
        }

    parts = {
        "hotels": [
            {"id": "t1", "city_id": "testcity", "name": "Т", "latitude": 43.6, "longitude": 39.7}
        ],
        "room_types": [
            {"id": "t1:sea", "hotel_id": "t1", "rg_hash": "sea", "name": "С видом"},
            {"id": "t1:std", "hotel_id": "t1", "rg_hash": "std", "name": "Стандарт"},
        ],
        "placement_sets": [
            {
                "room_type_id": rt,
                "completeness": "complete",
                "guarantee": "all_guaranteed",
                "status": "simulated_hotelier",
            }
            for rt in ("t1:sea", "t1:std")
        ],
        "placements": [
            {"id": "t1:sea|b|5", "room_type_id": "t1:sea", "building_id": "b", "floor": 5},
            {"id": "t1:std|b|2", "room_type_id": "t1:std", "building_id": "b", "floor": 2},
        ],
        "placement_windows": [
            {"placement_id": "t1:sea|b|5", "facade_id": "west"},
            {"placement_id": "t1:std|b|2", "facade_id": "east"},
        ],
        "facts": [
            fact(window_subject("b", "west", 5), "facade_physical_floor", "V05", True),
            fact(window_subject("b", "east", 2), "facade_physical_floor", "V05", False),
        ],
    }
    pack = tmp_path / "testcity"
    (pack / "data").mkdir(parents=True)
    files = []
    for kind, rows in parts.items():
        path = pack / "data" / f"{kind}.jsonl.gz"
        write_jsonl_gz(path, rows)
        files.append(
            PackFile(
                path=f"data/{kind}.jsonl.gz",
                kind=kind,
                rows=len(rows),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    manifest = Manifest(
        city_id="testcity",
        city_name="Тест",
        timezone="Europe/Moscow",
        depth="full",
        pack_version="t.1",
        built_at=datetime.now(UTC),
        files=files,
    )
    (pack / "manifest.json").write_text(manifest.model_dump_json(), "utf-8")
    with engine.begin() as conn:
        load_pack(conn, pack)

    db = DbStore(engine)
    rts = {rt["room_type_id"]: rt for rt in db.room_types(["t1"])["t1"]}
    assert rts["t1:sea"]["placements"][0]["windows"] == ["west"]
    assert rts["t1:sea"]["placement_set"]["completeness"] == "complete"

    ast = QueryAST(
        raw_text="закат",
        city_id="testcity",
        hard=[Condition(feature_id="V05", op="eq", value=True)],
    )
    r = search(db, ast)[0]
    assert r["status"] == "fits" and r["best_category"] == "t1:sea"

    # Новая версия пакета без размещений: старые размещения города убираются.
    for f in manifest.files:
        if f.kind in ("placement_sets", "placements", "placement_windows"):
            (pack / f.path).unlink()
    manifest.files = [
        f
        for f in manifest.files
        if f.kind not in ("placement_sets", "placements", "placement_windows")
    ]
    manifest.pack_version = "t.2"
    (pack / "manifest.json").write_text(manifest.model_dump_json(), "utf-8")
    with engine.begin() as conn:
        load_pack(conn, pack)
    assert all(rt["placement_set"] is None for rt in db.room_types(["t1"])["t1"])
    assert search(db, ast)[0]["hard"][0]["reason_code"] == "no_placements"
