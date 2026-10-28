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
def engine(tmp_path_factory):
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from pipeline.build_v9_packs import build
    from pipeline.load_packs import load_pack

    # Эти тесты сравнивают именно v9 с неизменными fixtures. Итоговые городские
    # пакеты уже шире когорты и имеют пересчитанные факты: их в PostGIS грузит
    # tests/test_shipped_packs_db.py (`make test-packs`).
    global PACKS
    PACKS = tmp_path_factory.mktemp("v9-contract-packs")
    build(PACKS)

    assert make_url(TEST_URL).database.endswith("_test"), "тест очищает базу — только *_test"
    eng = create_engine(TEST_URL)
    shadow = "seva_extension_" + uuid4().hex[:12]
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # Как в образе PostGIS: одноимённая таблица расширения в search_path.
        conn.execute(text(f"CREATE SCHEMA {shadow}"))
        conn.execute(text(f"CREATE TABLE {shadow}.place (gid integer)"))
    saved = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = (
        make_url(TEST_URL)
        .update_query_dict({"options": f"-csearch_path=public,{shadow}"})
        .render_as_string(hide_password=False)
    )
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        with eng.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {shadow} CASCADE"))
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
    from core.packs import iter_jsonl
    from pipeline.make_fixtures import LAYERS

    db, stub = stores
    hotels = [h["hotel_id"] for h in stub.hotels() if h["city_id"] in CITIES]
    assert hotels
    # В заглушках — ещё и факты слоёв калькуляторов (вид и солнце); v9-пакеты теста их не несут,
    # слой в базе проверяет tests/test_shipped_packs_db.py.
    layer = {r["fact_id"] for path in LAYERS if path.is_file() for r in iter_jsonl(path)}
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
            if f.fact_id not in layer
        }
        assert a == b, hotel_id


def test_facts_for_filters_params_in_db(stores):
    """Поиск отбирает факты по параметрам в SQL (даты заката — их тысячи на отель). Если с
    такими параметрами фактов нет, а с другими есть — один факт остаётся: поиск скажет
    «посчитано на другие параметры», а не «нет данных»."""
    db, _ = stores
    ids = [h["hotel_id"] for h in db.hotels("sochi")]
    hid, facts = next((h, fs) for h, fs in db.facts_for("L01", ids).items() if fs[0].params)
    want = facts[0].params
    got = db.facts_for("L01", [hid], params=want)[hid]
    assert got and all(f.params == want for f in got) and not got[0].sources  # без паспорта
    other = db.facts_for("L01", [hid], params={"arrival_local": "1999-01-01T00:00:00"})[hid]
    assert len(other) == 1
    keyed = db.facts_for("L01", [hid], params={}, key_params=("arrival_local",))[hid]
    assert len(keyed) == 1 and "arrival_local" in keyed[0].params
    assert db.fact(got[0].fact_id).sources  # полный факт — по id


def test_activated_feature_is_stored_and_listed(stores, monkeypatch):
    """Включение характеристики текстом на базе: спецификация и факты в одной транзакции,
    повторное включение пересчитывает, а не копит факты. В v9-пакетах мест нет — честное
    «неизвестно» с причиной."""
    from agents.catalog import catalog as catalog_module
    from agents.catalog import load_catalog
    from agents.specgen import draft_spec
    from agents.specgen.activate import activate

    monkeypatch.setattr(catalog_module, "_RUNTIME", {})
    db, _ = stores
    spec = draft_spec("сколько кафе в 500 м", use_llm=False).spec
    for _ in range(2):
        result = activate(spec, db, ("sochi",))
    assert result["activated"], result
    ids = [h["hotel_id"] for h in db.hotels("sochi")]
    facts = db.facts_for(spec.id, ids)
    assert sum(map(len, facts.values())) == len(ids) == result["facts"]
    assert {f.unknown_reason for fs in facts.values() for f in fs} == {"place_no_data"}
    (stored,) = [f for f in db.feature_specs() if f["id"] == spec.id]
    assert stored["cities"]["sochi"]["hotels"] == len(ids)
    load_catalog.cache_clear()


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
    r = search(db, ast)[0]
    # Без размещений — сводка по окнам корпусов (D40): «неизвестно», где окна «да».
    assert r["status"] != "fits" and r["hard"][0]["reason_code"] == "window_some"


def test_hotelier_loop_on_db(engine):
    """Вопрос → ответ имитатора → выдача меняется → настоящий ответ заменяет имитатор →
    противоречие с расчётом в журнале → размещения категории."""
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from agents.query_ast import Condition, QueryAST
    from core.db import Question, RoomType
    from core.hotelier import AnswerIn, build_answer
    from core.packs import stable_id
    from core.search import search
    from core.store_db import DbStore

    db, hid, now = DbStore(engine), "13736470", datetime.now(UTC)
    q = db.question(hid, None, "P01")
    assert q and q["answer_status"] == "not_asked"
    dog = Condition(feature_id="P01", op="eq", value="dog", field="species", binding="hotel")
    ast = QueryAST(raw_text="с собакой", city_id="sochi", hard=[dog])

    def status():
        return search(db, ast, only=[hid])[0]["status"]

    before = status()
    sim = build_answer(
        q,
        AnswerIn(question_id=q["question_id"], fields={"species": "dog"}, is_simulated=True),
        [],
        now,
    )
    db.apply_answer(sim)
    assert (before, status()) == ("needs_check", "fits")
    assert db.question(hid, q["question_id"], None)["answer_status"] == "answered"

    real = build_answer(
        q,
        AnswerIn(question_id=q["question_id"], fields={"species": "cat"}),
        db.facts_for("P01", [hid])[hid],
        now,
    )
    assert real.superseded == {sim.facts[0].fact_id: real.facts[0].fact_id}
    db.apply_answer(real)
    assert [f.fact_id for f in db.facts_for("P01", [hid])[hid]] == [real.facts[0].fact_id]
    assert status() == "not_fit"  # настоящий ответ: только кошки

    # Ответ отеля против нашего расчёта — противоречие в журнале, а не замена.
    calc = next(
        f
        for fs in db.hotel_facts(hid).values()
        for f in fs
        if f.status == "computed"
        and f.subject_level == "hotel"
        and isinstance(f.value, dict)
        and any(isinstance(v, int | float) for v in f.value.values())
    )
    key = next(k for k, v in calc.value.items() if isinstance(v, int | float))
    with engine.begin() as conn:
        conn.execute(
            insert(Question).values(
                id=stable_id("question", hid, calc.feature_id),
                hotel_id=hid,
                feature_id=calc.feature_id,
                subject_level="hotel",
                subject_id=f"hotel:{hid}",
                text="Проверка расчёта",
                reason="conflict",
                fields=[],
                prefill=[],
            )
        )
    q2 = db.question(hid, None, calc.feature_id)
    wrong = build_answer(
        q2,
        AnswerIn(
            question_id=q2["question_id"],
            fields={key: calc.value[key] + 1000},
            params=calc.params,  # тот же смысл, что у расчёта (D15)
        ),
        db.facts_for(calc.feature_id, [hid])[hid],
        now,
    )
    db.apply_answer(wrong)
    assert any(
        c["fact_a"] == calc.fact_id and c["fact_b"] == wrong.facts[0].fact_id
        for c in db.fact_conflicts("sochi")
    )
    assert calc.fact_id in {f.fact_id for f in db.facts_for(calc.feature_id, [hid])[hid]}

    # Этажи и стороны номеров категории (D17) — размещения.
    with engine.begin() as conn:
        conn.execute(
            insert(RoomType).values(
                id=f"{hid}:sea",
                hotel_id=hid,
                rg_hash="sea",
                name="С видом",
                amenities=[],
                source={},
            )
        )
    q3 = db.question(hid, None, "M02")
    placed = build_answer(
        q3,
        AnswerIn(
            question_id=q3["question_id"],
            room_type_id=f"{hid}:sea",
            is_simulated=True,
            placements={
                "completeness": "complete",
                "guarantee": "all_guaranteed",
                "items": [
                    {"building_id": "way/1", "floor": 5, "windows": ["west"]},
                    {"building_id": "way/1", "floor": 5, "windows": ["north"]},
                    {"building_id": "way/1", "floor": 6, "windows": ["west"]},
                ],
            },
        ),
        [],
        now,
    )
    db.apply_answer(placed)
    rt = next(r for r in db.room_types([hid])[hid] if r["room_type_id"] == f"{hid}:sea")
    assert rt["placement_set"]["status"] == "simulated_hotelier"
    assert [(p["floor"], p["windows"]) for p in rt["placements"]] == [
        (5, ["north", "west"]),
        (6, ["west"]),
    ]


def test_freshness_by_feature_and_recipe(stores):
    """Актуализация: у каждого значения есть дата (без своей — дата сборки пакета, с пометкой),
    срок обновления и чем обновить; «на дату» через два года устарело всё, кроме климата."""
    from datetime import UTC, datetime

    db, _ = stores
    now = db.freshness("sochi", datetime(2026, 10, 28, tzinfo=UTC))
    assert now and all(r["n"] > 0 and r["oldest"] and r["next_due"] for r in now)
    # Сразу «пора обновить» — только оценки «открыто в момент приезда»: v9 дал им сутки.
    assert {r["feature_id"] for r in now if r["stale"]} <= {"L01", "L03", "P04"}
    later = db.freshness("sochi", datetime(2028, 11, 1, tzinfo=UTC))
    assert sum(r["stale"] for r in later) == sum(r["n"] for r in later)
    undated = [f for fs in db.hotel_facts("13736470").values() for f in fs if f.status != "unknown"]
    kinds = {getattr(f, "date_kind", None) for f in undated if not f.observed_at}
    assert kinds <= {"pack_built_at", None} and all(
        f.calculated_at or f.observed_at for f in undated
    )
