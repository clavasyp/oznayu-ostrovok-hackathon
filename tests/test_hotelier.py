from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from core.facts import Fact
from core.hotelier import AnswerError, AnswerIn, build_answer

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
QUESTION = {
    "question_id": 1,
    "hotel_id": "h1",
    "city_id": "sochi",
    "feature_id": "P01",
    "parsed_fields": ["species", "weight_kg", "number"],
}
RADISSON = "13736470"
DOG = {"feature_id": "P01", "op": "eq", "value": "dog", "field": "species", "binding": "hotel"}


def fact(fid, status, value, feature="P01"):
    return Fact(
        fact_id=fid,
        hotel_id="h1",
        city_id="sochi",
        subject_level="hotel",
        subject_id="hotel:h1",
        feature_id=feature,
        value=value,
        status=status,
    )


def test_answer_becomes_hotelier_fact_with_passport():
    r = build_answer(
        QUESTION,
        AnswerIn(question_id=1, answer_text="Собаку до 10 кг — да", fields={"species": "dog"}),
        [],
        NOW,
    )
    (f,) = r.facts
    assert r.state == "answered" and f.status == "hotelier" and f.value == {"species": "dog"}
    assert f.sources[0].provider == "hotelier"
    assert f.sources[0].model_extra["quote"] == "Собаку до 10 кг — да"


def test_hotel_claim_replaces_ostrovok_but_not_our_computation():
    existing = [
        fact("ota", "ostrovok_content", {"species": "cat"}),
        fact("calc", "computed", {"species": "cat"}),
    ]
    r = build_answer(QUESTION, AnswerIn(question_id=1, fields={"species": "dog"}), existing, NOW)
    new = r.facts[0].fact_id
    assert r.superseded == {"ota": new}  # заявление отеля обновило его же заявление
    assert [(a.fact_id, b.fact_id) for a, b in r.conflicts] == [("calc", new)]  # а с расчётом спор


def test_answer_with_other_params_is_another_meaning():
    existing = [fact("calc", "computed", {"count": 3})]
    existing[0].params = {"radius_m": 300}
    other = build_answer(
        QUESTION | {"parsed_fields": []},
        AnswerIn(question_id=1, fields={"count": 9}),
        existing,
        NOW,
    )
    assert other.conflicts == []  # без радиуса это другой смысл — не сравниваем (D15)
    same = build_answer(
        QUESTION | {"parsed_fields": []},
        AnswerIn(question_id=1, fields={"count": 9}, params={"radius_m": 300}),
        existing,
        NOW,
    )
    assert [a.fact_id for a, _ in same.conflicts] == ["calc"]


def test_simulator_does_not_override_real_hotelier():
    existing = [fact("real", "hotelier", {"species": "cat"})]
    answer = AnswerIn(question_id=1, fields={"species": "dog"}, is_simulated=True)
    r = build_answer(QUESTION, answer, existing, NOW)
    assert r.facts[0].status == "simulated_hotelier" and r.superseded == {}


def test_fields_outside_question_are_rejected():
    with pytest.raises(AnswerError, match="hvac"):
        build_answer(QUESTION, AnswerIn(question_id=1, fields={"hvac": True}), [], NOW)


def test_dont_know_is_unknown_not_no():
    existing = [fact("ota", "ostrovok_content", {"species": "cat"})]
    r = build_answer(QUESTION, AnswerIn(question_id=1, dont_know=True), existing, NOW)
    assert r.state == "answered_unknown" and r.facts[0].status == "unknown"
    assert r.superseded == {}  # «не знаю» не отменяет известного


def test_text_without_fields_waits_for_parser():
    r = build_answer(QUESTION, AnswerIn(question_id=1, answer_text="Да, можно"), [], NOW)
    assert r.state == "answered_unparsed" and r.facts == []


def test_placements_need_category():
    placements = {
        "completeness": "complete",
        "guarantee": "all_guaranteed",
        "items": [{"building_id": "b", "floor": 5, "windows": ["west"]}],
    }
    with pytest.raises(ValidationError):
        AnswerIn(question_id=1, placements=placements)


def test_answer_changes_search_status_in_fixtures_mode():
    with TestClient(create_app()) as c:
        q = next(
            q
            for q in c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
            if q["feature_id"] == "P01"
        )
        assert (
            c.post(f"/api/hotelier/{RADISSON}/questions/{q['question_id']}/ask").status_code == 200
        )
        ast = {"raw_text": "с собакой", "city_id": "sochi", "hard": [DOG]}
        body = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={
                "question_id": q["question_id"],
                "answer_text": "С собакой можно",
                "fields": {"species": "dog", "weight_kg": 10},
                "is_simulated": True,
                "ast": ast,
            },
        ).json()
        assert body["state"] == "answered" and body["facts"][0]["status"] == "simulated_hotelier"
        assert body["before"]["status"] == "needs_check"  # раньше не знали
        assert body["after"]["status"] == "fits"  # ответ изменил выдачу
        assert any("имитатор" in n for n in body["notes"])
        questions = c.get(f"/api/hotelier/{RADISSON}/questions").json()["questions"]
        assert next(x for x in questions if x["feature_id"] == "P01")["answer_status"] == "answered"
        conflicts = c.get("/api/conflicts", params={"city_id": "sochi"}).json()
        assert set(conflicts) >= {"conflicts", "fact_conflicts"}
        found = c.post("/api/search", json={"ast": ast}).json()["results"]
        assert next(r for r in found if r["hotel"]["hotel_id"] == RADISSON)["status"] == "fits"


def test_answer_errors():
    with TestClient(create_app()) as c:
        assert (
            c.post(
                f"/api/hotelier/{RADISSON}/answers", json={"feature_id": "ZZZ", "dont_know": True}
            ).status_code
            == 404
        )
        bad = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={"feature_id": "P01", "fields": {"not_a_field": 1}},
        )
        assert bad.status_code == 422 and "not_a_field" in bad.json()["detail"]
        no_category = c.post(
            f"/api/hotelier/{RADISSON}/answers",
            json={
                "feature_id": "M02",
                "room_type_id": f"{RADISSON}:nope",
                "placements": {
                    "completeness": "complete",
                    "guarantee": "all_guaranteed",
                    "items": [{"building_id": "b", "floor": 5, "windows": ["west"]}],
                },
            },
        )
        assert no_category.status_code == 422
