import pytest
from fastapi.testclient import TestClient

from agents.catalog import load_catalog
from agents.hotelier import parse_answer
from agents.hotelier.parse_answer import validate
from agents.hotelier.schemas import AnswerCompact, FieldAnswer, answer_schema
from agents.hotelier.simulator import simulate_answer
from agents.hotelier.to_fact import answer_fact, conflicts_with_ostrovok
from agents.llm import Gateway
from agents.llm.fake import FakeLLM
from app.main import create_app

RADISSON, ARCHITECT, COSMOS = "13736470", "10012084", "9986868"
OFFLINE = Gateway([])
CAT = load_catalog()


@pytest.fixture
def client():
    with TestClient(create_app()) as c:  # своё приложение: тесты дописывают факты в хранилище
        yield c


def status(client, hotel_id, *hard):
    body = {"raw_text": "тест", "city_id": "sochi", "hard": list(hard)}
    results = client.post("/api/search", json={"ast": body}).json()["results"]
    return next(r for r in results if r["hotel"]["hotel_id"] == hotel_id)["status"]


def cond(feature_id, field, op="eq", value=True):
    return {"feature_id": feature_id, "field": field, "op": op, "value": value, "binding": "hotel"}


def ask_simulator(client, hotel_id, feature_id):
    sim = client.post(f"/api/hotelier/{hotel_id}/simulate", json={"feature_id": feature_id}).json()
    assert sim["is_simulated"] is True and sim["answer_text"]
    payload = {"feature_id": feature_id, "answer_text": sim["answer_text"], "is_simulated": True}
    return client.post(f"/api/hotelier/{hotel_id}/answers", json=payload).json()


def test_question_with_ostrovok_prefill(client):
    body = client.get(f"/api/hotelier/{RADISSON}/questions").json()
    qs = {q["feature_id"]: q for q in body["questions"]}
    assert len(qs) == 26
    assert qs["S04"]["prefill"].startswith("На странице отеля на Островке указано: «Кондиционер»")
    assert qs["S04"]["prefill_checks_fields"] == ["has_ac"]
    assert {f["name"] for f in qs["A03"]["fields"]} >= {"has_lift", "door_width_mm"}
    assert {q["room_type_id"] for q in body["placement_questions"]} == {
        "13736470:3502302001O",
        "13736470:3202302001F",
    }


@pytest.mark.parametrize(
    ("hotel_id", "feature_id", "condition", "after"),
    [
        (RADISSON, "P01", cond("P01", "pets_allowed"), "fits"),
        (RADISSON, "P01", cond("P01", "max_weight_kg", "gte", 15), "not_fit"),  # в профиле до 10 кг
        (ARCHITECT, "L04", cond("L04", "checkin_possible"), "fits"),
        (ARCHITECT, "L04", cond("L04", "front_desk_24h"), "not_fit"),
        (COSMOS, "A03", cond("A03", "has_lift"), "fits"),
        (COSMOS, "P01", cond("P01", "pets_allowed"), "not_fit"),
    ],
)
def test_simulated_answer_changes_search_status(client, hotel_id, feature_id, condition, after):
    assert status(client, hotel_id, condition) == "needs_check"
    result = ask_simulator(client, hotel_id, feature_id)
    assert result["accepted"] and result["fact"]["status"] == "simulated_hotelier"
    assert result["fact"]["is_simulated"] is True
    assert status(client, hotel_id, condition) == after


def test_yes_lift_does_not_fill_lift_size(client):
    """Cosmos отвечает расплывчато: «Да, лифт есть» — размеры не заполняются, нужен вопрос."""
    result = ask_simulator(client, COSMOS, "A03")
    assert set(result["fields"]) == {"has_lift"}
    assert "ширина двери лифта" in result["followup"]
    assert status(client, COSMOS, cond("A03", "door_width_mm", "gte", 800)) == "needs_check"


def test_placement_answer_becomes_fact(client):
    rt = "13736470:3502302001O"
    sim = client.post(
        f"/api/hotelier/{RADISSON}/simulate",
        json={"feature_id": "category_placement", "room_type_id": rt},
    ).json()
    payload = {
        "feature_id": "category_placement",
        "room_type_id": rt,
        "answer_text": sim["answer_text"],
        "is_simulated": True,
    }
    fact = client.post(f"/api/hotelier/{RADISSON}/answers", json=payload).json()["fact"]
    (p,) = fact["value"]["placements"]
    assert p["side"] == "SW" and p["floors"] == [3, 4, 5, 6, 7, 8] and p["azimuth_deg"] == 225
    assert fact["subject_id"] == rt and fact["hard_condition_eligible"] is True


def test_validator_rejects_guesses():
    a03 = CAT.get("A03")
    answer = "Да, лифт есть."
    compact = AnswerCompact(
        answer_state="answered",
        quantifier="unknown",
        fields=[
            FieldAnswer(name="has_lift", value="true", unit="", quote="Да, лифт есть"),
            FieldAnswer(name="door_width_mm", value="900", unit="мм", quote="лифт есть"),
            FieldAnswer(name="cabin_width_mm", value="1100", unit="мм", quote="кабина 110 см"),
            FieldAnswer(name="no_such", value="1", unit="", quote="Да"),
        ],
        conditions="",
        unclear=[],
    )
    parsed = validate(a03, answer, compact)
    assert set(parsed.accepted) == {"has_lift"}
    reasons = {r["field"]: r["reason"] for r in parsed.rejected}
    assert reasons["door_width_mm"] == "числа нет в цитате"
    assert reasons["cabin_width_mm"] == "цитаты нет в ответе"
    assert "нет такого поля" in reasons["no_such"]


def test_domain_and_units_checked():
    p01 = CAT.get("P01")
    parsed = parse_answer("P01", "Да, с животными можно. Вес до 500 кг.", gateway=OFFLINE)
    assert parsed.accepted["pets_allowed"].value is True
    assert "вне области" in parsed.rejected[0]["reason"]
    a02 = CAT.get("A02")
    parsed = parse_answer("A02", "Да, вход без ступеней, ширина прохода 90 см.", gateway=OFFLINE)
    assert parsed.accepted["clear_width_mm"].value == 900 and a02 and p01


def test_llm_path_uses_schema_and_same_validation():
    reply = {
        "answer_state": "answered",
        "quantifier": "all",
        "fields": [
            {"name": "has_ac", "value": "true", "unit": "", "quote": "кондиционеры есть везде"},
            {"name": "individual_control", "value": "true", "unit": "", "quote": "пульт"},
        ],
        "conditions": "",
        "unclear": [],
    }
    fake = FakeLLM([reply])
    parsed = parse_answer(
        "S04", "Да, кондиционеры есть везде, пульт в каждом номере.", gateway=Gateway([fake])
    )
    assert parsed.parsed_by == "llm" and fake.calls[0]["schema"] == answer_schema()
    assert parsed.accepted["has_ac"].value is True and parsed.quantifier == "all"
    assert parsed.accepted["individual_control"].quote == "пульт"


def test_unknown_answer_gives_no_fact_and_followup():
    parsed = parse_answer("N05", simulate_answer(RADISSON, "N05")["answer_text"], gateway=OFFLINE)
    assert parsed.answer_state == "unknown" and not parsed.accepted and parsed.followup
    fact = answer_fact(
        hotel_id=RADISSON,
        city_id="sochi",
        feature=CAT.get("N05"),
        parsed=parsed,
        question_id="q",
        is_simulated=True,
    )
    assert fact is None


def test_some_rooms_do_not_close_hard_condition():
    parsed = parse_answer("M02", "Да, балкон есть, но не во всех номерах.", gateway=OFFLINE)
    fact = answer_fact(
        hotel_id=RADISSON,
        city_id="sochi",
        feature=CAT.get("M02"),
        parsed=parsed,
        question_id="q",
        is_simulated=False,
    )
    assert fact.status == "hotelier" and fact.quantifier == "some"
    assert fact.usable_in_hard is False


def test_conflict_with_ostrovok_card():
    parsed = parse_answer("S04", "Нет, кондиционеров в номерах нет.", gateway=OFFLINE)
    fact = answer_fact(
        hotel_id=RADISSON,
        city_id="sochi",
        feature=CAT.get("S04"),
        parsed=parsed,
        question_id="q",
        is_simulated=True,
    )
    claims = [{"atom_id": "cooling_presence", "value": True, "quote": "Кондиционер"}]
    (c,) = conflicts_with_ostrovok(fact, claims)
    assert c["field"] == "has_ac" and c["state"] == "detected" and c["ota_value"] == "Кондиционер"


def test_conflict_keeps_condition_unconfirmed(client):
    payload = {"feature_id": "S04", "answer_text": "Нет, кондиционеров нет.", "is_simulated": True}
    result = client.post(f"/api/hotelier/{RADISSON}/answers", json=payload).json()
    assert result["conflicts"] and result["fact"]["hard_condition_eligible"] is False
    assert status(client, RADISSON, cond("S04", "has_ac")) == "needs_check"
    conflicts = client.get("/api/conflicts?city_id=sochi").json()["conflicts"]
    assert any(c.get("classification") == "hotelier_vs_ostrovok_card" for c in conflicts)


def test_questions_copy_matches_v9():
    from pathlib import Path

    from agents.hotelier.questions import QUESTIONS_CSV

    v9 = Path("research_notes/Расчёт характеристик/hotelier/questions_26.csv")
    assert QUESTIONS_CSV.read_bytes() == v9.read_bytes()
