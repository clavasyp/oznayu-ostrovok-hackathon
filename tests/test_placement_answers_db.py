"""Ответ о размещении в режиме db: названия корпусов и размещения после новой версии пакета,
ответ имитатора при настоящем ответе (PostGIS)."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="нужна PostGIS")


@pytest.fixture
def engine(monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = os.environ["TEST_DATABASE_URL"]
    assert make_url(url).database.endswith("_test")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_MODE", "db")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    yield engine
    engine.dispose()


def test_placement_answer_saved_and_alias_survives_pack_reload(tmp_path, engine):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.main import create_app
    from core.db import PlacementSet, Question
    from core.packs import Manifest, PackFile, sha256_file
    from core.store_db import DbStore
    from pipeline.build_v9_packs import write_jsonl_gz
    from pipeline.load_packs import load_pack

    city = "placement_test_" + uuid4().hex
    hid, bid, rt = city + "hotel", city + "building", city + "hotel:rg1"
    parts = {
        "hotels": [
            {"id": hid, "city_id": city, "name": "Test hotel", "latitude": 55.7, "longitude": 37.6}
        ],
        "room_types": [
            {
                "id": rt,
                "hotel_id": hid,
                "rg_hash": "rg1",
                "name": "Стандарт",
                "amenities": [],
                "source": {},
            }
        ],
        "buildings": [
            {
                "id": bid,
                "city_id": city,
                "tags": {"addr:street": "Тестовая улица", "addr:housenumber": "1"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[37.6, 55.7], [37.601, 55.7], [37.601, 55.701], [37.6, 55.7]]],
                },
            }
        ],
        "hotel_buildings": [
            {"hotel_id": hid, "building_id": bid, "method": "auto", "evidence": {"rule": "first"}}
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
                "geometry": {"type": "LineString", "coordinates": [[37.6, 55.7], [37.6, 55.701]]},
            }
        ],
    }

    def write(version):
        pack = tmp_path / version
        (pack / "data").mkdir(parents=True)
        files = []
        for kind, records in sorted(parts.items()):
            path = pack / "data" / (kind + ".jsonl.gz")
            write_jsonl_gz(path, records)
            files.append(
                PackFile(
                    path="data/" + path.name,
                    kind=kind,
                    rows=len(records),
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
        manifest = Manifest(
            city_id=city,
            city_name="Placement test",
            timezone="Europe/Moscow",
            depth="full",
            pack_version=version,
            built_at=datetime(2026, 10, 27, tzinfo=UTC),
            files=files,
        )
        (pack / "manifest.json").write_text(manifest.model_dump_json())
        return pack

    with engine.begin() as conn:
        assert "загружено" in load_pack(conn, write("first"))
    with engine.connect() as conn:
        questions_before = conn.execute(select(Question.id, Question.state)).all()
    text = "Окна на запад, с 2 по 4 этаж (корпус А). Других вариантов нет. Гарантируем."
    answer = {"room_type_id": rt, "answer_text": text, "building_aliases": {"корпус А": bid}}
    with TestClient(create_app(ui_dist=tmp_path / "no-ui")) as client:
        body = client.post(f"/api/hotelier/{hid}/placement-answers", json=answer).json()
        assert body["state"] == "answered" and body["stub"] is False
        placed = body["room_type"]
        assert placed["placement_set"]["status"] == "hotelier"
        assert placed["placement_set"]["source"]["question_id"] == body["question_id"]
        assert [(p["floor"], p["windows"]) for p in placed["placements"]] == [
            (2, ["west"]), (3, ["west"]), (4, ["west"])
        ]  # fmt: skip
        # Ответ имитатора (песочница, D41-8) не заменяет настоящий ответ.
        sim = {"room_type_id": rt, "answer_text": "Окна на запад, с 7 по 8 этаж.",
               "is_simulated": True}  # fmt: skip
        kept = client.post(f"/api/hotelier/{hid}/placement-answers", json=sim).json()
        assert kept["state"] == "real_answer_kept" and kept["real_answer_kept"] is True
        assert kept["room_type"]["placement_set"]["status"] == "hotelier"
        assert [p["floor"] for p in kept["room_type"]["placements"]] == [2, 3, 4]
        state = client.get(f"/api/hotelier/{hid}/placement-questions").json()["questions"][0]
        assert (state["answer_status"], state["placement_status"]) == ("answered", "hotelier")
    # Вопрос о размещении (`qp_…`) — не строка таблицы вопросов: её ответ не трогает.
    with engine.connect() as conn:
        assert conn.execute(select(Question.id, Question.state)).all() == questions_before
    (alias,) = body["aliases_saved"]
    assert alias["source"]["method"] == "selected_in_list"
    db = DbStore(engine)
    assert not db.add_building_alias(hid, city + "missing", alias)
    assert db.placement_geometry(hid)["placement_building_aliases"]["корпус а"] == bid

    # Новая версия пакета: названия и размещения со слов отеля остаются (FORMAT.md).
    parts["hotel_buildings"][0]["evidence"] = {"rule": "second"}
    with engine.begin() as conn:
        msg = load_pack(conn, write("second"))
    assert "'hotel_aliases_kept': 1" in msg
    assert "'placements_kept': 1, 'placements_dropped': 0" in msg
    (link,) = db.placement_geometry(hid)["hotel_buildings"]
    assert link["evidence"]["rule"] == "second"
    assert [(a["name"], a["status"]) for a in link["evidence"]["hotel_aliases"]] == [
        ("корпус А", "hotelier")
    ]
    assert db.placement_geometry(hid)["placement_building_aliases"]["корпус а"] == bid
    (after,) = db.room_types([hid])[hid]
    assert after["placement_set"] == placed["placement_set"]
    assert after["placements"] == placed["placements"]

    # Стена изменилась (новый `seg-…`) — размещение на ней не переносится, его считаем.
    parts["facades"][0]["facade_id"] = "west-moved"
    with engine.begin() as conn:
        msg = load_pack(conn, write("third"))
    assert "'placements_kept': 0, 'placements_dropped': 1" in msg
    (gone,) = db.room_types([hid])[hid]
    assert gone["placement_set"] is None and gone["placements"] == []
    with engine.connect() as conn:
        assert conn.scalar(select(PlacementSet.room_type_id).where(
            PlacementSet.room_type_id == rt)) is None  # fmt: skip


def test_simulated_set_survives_reload_but_does_not_replace_pack_curated(tmp_path, engine):
    """Набор имитатора переживает новую версию пакета, но не заменяет набор команды из пакета;
    набор из пакета новой версией заменяется (у него `source.pack`)."""
    from datetime import UTC, datetime

    from core.hotelier import AnswerIn, build_answer
    from core.packs import Manifest, PackFile, sha256_file
    from core.store_db import DbStore
    from pipeline.build_v9_packs import write_jsonl_gz
    from pipeline.load_packs import load_pack

    city = "placement_sim_" + uuid4().hex
    hid, bid = city + "hotel", city + "building"
    rts = [f"{hid}:a", f"{hid}:b"]
    parts = {
        "hotels": [{"id": hid, "city_id": city, "name": "Т", "latitude": 55.7, "longitude": 37.6}],
        "room_types": [
            {"id": rt, "hotel_id": hid, "rg_hash": rt[-1], "name": rt[-1], "amenities": [],
             "source": {}}
            for rt in rts
        ],
        "buildings": [{"id": bid, "city_id": city, "tags": {}, "geometry": {
            "type": "Polygon",
            "coordinates": [[[37.6, 55.7], [37.601, 55.7], [37.601, 55.701], [37.6, 55.7]]]}}],
        "hotel_buildings": [{"hotel_id": hid, "building_id": bid, "method": "auto",
                             "evidence": {}}],
        "facades": [{"building_id": bid, "facade_id": "s", "azimuth_deg": 180, "side": "S",
                     "is_courtyard": None, "length_m": 20, "source": {},
                     "geometry": {"type": "LineString",
                                  "coordinates": [[37.6, 55.7], [37.601, 55.7]]}}],
    }  # fmt: skip

    def write(version, extra=None):
        pack = tmp_path / version
        (pack / "data").mkdir(parents=True)
        files = []
        for kind, records in sorted((parts | (extra or {})).items()):
            path = pack / "data" / (kind + ".jsonl.gz")
            write_jsonl_gz(path, records)
            files.append(PackFile(path="data/" + path.name, kind=kind, rows=len(records),
                                  bytes=path.stat().st_size, sha256=sha256_file(path)))  # fmt: skip
        manifest = Manifest(city_id=city, city_name="Т", timezone="Europe/Moscow", depth="full",
                            pack_version=version, built_at=datetime(2026, 10, 27, tzinfo=UTC),
                            files=files)  # fmt: skip
        (pack / "manifest.json").write_text(manifest.model_dump_json())
        return pack

    with engine.begin() as conn:
        load_pack(conn, write("v1"))
    db = DbStore(engine)
    now = datetime.now(UTC)
    for rt in rts:
        q = {"question_id": "qp_x", "hotel_id": hid, "feature_id": "category_placement",
             "city_id": city}  # fmt: skip
        answer = AnswerIn(feature_id="category_placement", room_type_id=rt, is_simulated=True,
                          placements={"completeness": "complete", "guarantee": "all_guaranteed",
                                      "items": [{"building_id": bid, "floor": 3, "windows": ["s"],
                                                 "room_variant": "s"}]})  # fmt: skip
        db.apply_answer(build_answer(q, answer, [], now))
    # v2: команда положила в пакет свой набор для категории «a».
    curated = {
        "placement_sets": [{"room_type_id": rts[0], "completeness": "complete",
                            "guarantee": "all_guaranteed", "status": "curated", "source": {}}],
        "placements": [{"id": f"{rts[0]}|{bid}|5", "room_type_id": rts[0], "building_id": bid,
                        "floor": 5}],
        "placement_windows": [{"placement_id": f"{rts[0]}|{bid}|5", "facade_id": "s"}],
    }  # fmt: skip
    with engine.begin() as conn:
        msg = load_pack(conn, write("v2", curated))
    assert "'placements_kept': 1, 'placements_dropped': 1" in msg
    got = {rt["room_type_id"]: rt for rt in db.room_types([hid])[hid]}
    assert got[rts[0]]["placement_set"]["status"] == "curated"
    assert got[rts[0]]["placement_set"]["source"] == {"pack": "v2"}
    assert [p["floor"] for p in got[rts[0]]["placements"]] == [5]
    assert got[rts[1]]["placement_set"]["status"] == "simulated_hotelier"
    assert got[rts[1]]["placements"][0]["placement_id"] == f"{rts[1]}|{bid}|3|s"
    # v3 без набора команды: набор пакета уходит, набор имитатора — остаётся.
    with engine.begin() as conn:
        msg = load_pack(conn, write("v3"))
    got = {rt["room_type_id"]: rt for rt in db.room_types([hid])[hid]}
    assert got[rts[0]]["placement_set"] is None
    assert got[rts[1]]["placement_set"]["status"] == "simulated_hotelier"
