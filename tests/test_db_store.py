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
