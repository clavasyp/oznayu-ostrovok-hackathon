"""Id для браузера и чистые пакеты.

- `question_id` в ответах API — строка: id вопросов 60-битные, а браузер читает числа JSON как
  double и округляет после 2^53. Клиент «как в браузере» должен пройти весь цикл отельера.
- В фактах пакетов и заглушек нет путей с машины разработчика; пути к файлам расчёта — не в
  `params` (D15), а в `evidence_files`, от корня репозитория.
"""

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.packs import PROVENANCE_PARAMS, PackError, discover, move_provenance_params
from pipeline.build_v9_packs import PACKS
from pipeline.make_fixtures import OUT as FIXTURES

RADISSON = "13736470"
SAFE = 2**53


def as_browser(resp):
    """JSON так, как его читает браузер: все целые числа — double."""
    return json.loads(resp.text, parse_int=float)


def big_ints(x):
    if isinstance(x, bool):
        return []
    if isinstance(x, int):
        return [x] if abs(x) > SAFE else []
    if isinstance(x, dict):
        return [i for v in x.values() for i in big_ints(v)]
    if isinstance(x, list):
        return [i for v in x for i in big_ints(v)]
    return []


def test_hotelier_cycle_works_from_a_browser():
    with TestClient(create_app()) as c:
        questions = as_browser(c.get(f"/api/hotelier/{RADISSON}/questions"))["questions"]
        q = next(x for x in questions if x["feature_id"] == "P01")
        qid = q["question_id"]
        assert isinstance(qid, str) and int(qid) > SAFE  # число бы округлилось

        asked = c.post(f"/api/hotelier/{RADISSON}/questions/{qid}/ask")
        assert asked.status_code == 200 and as_browser(asked)["question"]["question_id"] == qid

        sim = as_browser(c.post(f"/api/hotelier/{RADISSON}/simulate", json={"question_id": qid}))
        assert sim["question_id"] == qid

        answered = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={"question_id": qid, "answer_text": sim["answer_text"], "is_simulated": True},
        )
        body = as_browser(answered)
        assert answered.status_code == 200 and body["state"] == "answered"
        assert body["question"]["question_id"] == qid
        assert body["facts"][0]["sources"][0]["question_id"] == qid


def test_no_unsafe_integers_in_api_responses():
    with TestClient(create_app()) as c:
        qid = c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"][0]["question_id"]
        responses = [
            c.get(f"/api/hotelier/{RADISSON}/questions"),
            c.post(f"/api/hotelier/{RADISSON}/questions/{qid}/ask"),
            c.post(
                f"/api/hotelier/{RADISSON}/answers", json={"question_id": qid, "dont_know": True}
            ),
            c.get(f"/api/hotels/{RADISSON}"),
            c.post("/api/search", json={"text": "с собакой", "city_id": "sochi"}),
            c.get("/api/conflicts", params={"city_id": "sochi"}),
        ]
    for r in responses:
        assert r.status_code == 200 and big_ints(r.json()) == []


def test_numeric_question_id_still_accepted():
    with TestClient(create_app()) as c:
        qid = c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"][0]["question_id"]
        r = c.post(
            f"/api/hotelier/{RADISSON}/answers", json={"question_id": int(qid), "dont_know": True}
        )
    assert r.status_code == 200 and r.json()["question"]["question_id"] == qid


def test_provenance_paths_leave_params():
    fact = {
        "fact_id": "f1",
        "params": {"month": 7, "fan_file": "/Users/dev/Хакатон/research_notes/v9/fan.jsonl"},
        "sources": [
            {"provider": "osm", "snapshot_file": "/Users/dev/Хакатон/research_notes/a.json"}
        ],
    }
    out = move_provenance_params(fact)
    assert out["params"] == {"month": 7}  # смысл условия остался, путь ушёл
    empty = move_provenance_params(
        {"fact_id": "f3", "params": {"quality_exclusion_evidence": None}}
    )
    assert empty["params"] == {} and "evidence_files" not in empty
    assert out["evidence_files"] == {"fan_file": "research_notes/v9/fan.jsonl"}
    assert out["sources"][0]["snapshot_file"] == "research_notes/a.json"
    with pytest.raises(PackError, match="params"):
        move_provenance_params({"fact_id": "f2", "params": {"other": "/Users/dev/x.json"}})


FACT_FILES = [
    *(p / "data" / "facts.jsonl.gz" for p in discover(PACKS)),
    FIXTURES / "facts.jsonl.gz",
]
LOCAL_ROOTS = ("/Users/", "/home/", "/private/", "/tmp/")


@pytest.mark.parametrize("path", FACT_FILES, ids=lambda p: p.parent.parent.name)
def test_facts_have_no_local_paths(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            assert not any(root in line for root in LOCAL_ROOTS), line[:200]
            params = json.loads(line).get("params") or {}
            assert not set(params) & set(PROVENANCE_PARAMS)  # и пустых ключей с путями нет


def test_placement_passport_question_id_is_string():
    # Ответ с размещениями кладёт id вопроса в паспорт размещений категории (GET /api/hotels).
    app = create_app()
    with TestClient(app) as c:
        app.state.store.room_types_by_hotel[RADISSON] = [
            {"room_type_id": "rt1", "name": "Стандарт", "placement_set": None, "placements": []}
        ]
        q = next(
            x
            for x in c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
            if x["feature_id"] == "P01"
        )
        placements = {
            "completeness": "complete",
            "guarantee": "all_guaranteed",
            "items": [{"building_id": "b", "floor": 5, "windows": ["west"]}],
        }
        answered = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={"question_id": q["question_id"], "room_type_id": "rt1", "placements": placements},
        )
        hotel = c.get(f"/api/hotels/{RADISSON}").json()
    assert answered.status_code == 200 and big_ints(hotel) == []
    (rt,) = hotel["room_types"]
    assert rt["placement_set"]["source"]["question_id"] == q["question_id"]
