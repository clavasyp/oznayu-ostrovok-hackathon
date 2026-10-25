import csv
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.catalog import load_catalog
from agents.hotelier import answer_from_text, parse_answer, simulate_answer
from agents.hotelier.parse_answer import validate
from agents.hotelier.placement import parse_placement, to_placements
from agents.hotelier.questions import QUESTIONS_CSV
from agents.hotelier.schemas import AnswerCompact, FieldAnswer, answer_schema
from agents.hotelier.simulator import load_profile, simulate_placement
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


def cond(feature_id, field, op="eq", value=True):
    return {"feature_id": feature_id, "field": field, "op": op, "value": value, "binding": "hotel"}


def ask_simulator(client, hotel_id, feature_id, ast=None):
    """Вопрос → ответ имитатора → агент разбирает текст → core/hotelier → факт."""
    sim = client.post(f"/api/hotelier/{hotel_id}/simulate", json={"feature_id": feature_id})
    sim = sim.json()
    assert sim["is_simulated"] is True and sim["answer_text"]
    payload = {
        "question_id": sim["question_id"],
        "answer_text": sim["answer_text"],
        "is_simulated": True,
    }
    if ast:
        payload["ast"] = ast
    return client.post(f"/api/hotelier/{hotel_id}/answers", json=payload).json()


def test_catalog_fields_are_question_fields():
    with QUESTIONS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            assert list(CAT.get(row["feature_id"]).fields) == row["parsed_fields"].split(",")


def test_profiles_use_question_fields():
    for hotel_id in (RADISSON, ARCHITECT, COSMOS):
        for fid, entry in load_profile(hotel_id)["answers"].items():
            assert set(entry.get("fields", {})) <= set(CAT.get(fid).fields), (hotel_id, fid)


def test_questions_get_prefill_and_fields(client):
    body = client.get(f"/api/hotelier/{RADISSON}/questions").json()
    qs = {q["feature_id"]: q for q in body["questions"]}
    assert len(qs) == 26
    assert qs["S04"]["prefill"].startswith("На странице отеля на Островке указано: «Кондиционер»")
    assert [f["name"] for f in qs["A03"]["fields"]] == qs["A03"]["parsed_fields"]
    assert next(f for f in qs["A03"]["fields"] if f["name"] == "lift_id")["absent_value"] == "none"


@pytest.mark.parametrize(
    ("hotel_id", "feature_id", "condition", "after"),
    [
        (RADISSON, "P01", cond("P01", "species", "in", ["dog", "dog_cat", "any"]), "fits"),
        (RADISSON, "P01", cond("P01", "weight_kg", "gte", 15), "not_fit"),  # в профиле до 10 кг
        (ARCHITECT, "S04", cond("S04", "hvac_type", "ne", "none"), "fits"),
        (ARCHITECT, "A03", cond("A03", "lift_id", "ne", "none"), "not_fit"),
        (COSMOS, "A03", cond("A03", "lift_id", "ne", "none"), "fits"),
        (COSMOS, "P01", cond("P01", "species", "in", ["dog", "dog_cat", "any"]), "not_fit"),
    ],
)
def test_simulated_answer_changes_search_status(client, hotel_id, feature_id, condition, after):
    ast = {"raw_text": "тест", "city_id": "sochi", "hard": [condition]}
    body = ask_simulator(client, hotel_id, feature_id, ast)
    assert body["state"] == "answered" and body["agent"]["parsed_by"] == "rules"
    assert body["facts"][0]["status"] == "simulated_hotelier"
    assert body["before"]["status"] == "needs_check" and body["after"]["status"] == after


def test_night_checkin_desk_or_self_checkin(client):
    """«Заселение ночью» = круглосуточная стойка ИЛИ самозаселение (OR в AST)."""
    search = {"text": "нужно заселение ночью", "city_id": "sochi"}
    before = client.post("/api/search", json=search).json()
    assert before["ast"]["logic"] and before["ast"]["hard_root"] == "root"
    for hotel_id in (ARCHITECT, RADISSON):  # самозаселение по коду / стойка 24/7
        assert ask_simulator(client, hotel_id, "L04")["state"] == "answered"
    results = client.post("/api/search", json=search).json()["results"]
    found = {r["hotel"]["hotel_id"]: r["status"] for r in results}
    assert found[ARCHITECT] == "fits" and found[RADISSON] == "fits"
    assert found[COSMOS] == "needs_check"  # не спрашивали


def test_yes_lift_does_not_fill_lift_size(client):
    """Cosmos отвечает расплывчато: «Да, лифт есть» — размеры не заполняются, нужен вопрос."""
    body = ask_simulator(client, COSMOS, "A03")
    assert body["facts"][0]["value"] == {"lift_id": "present"}
    assert "ширина двери лифта" in body["agent"]["followup"]


def test_unknown_answer_is_dont_know(client):
    body = ask_simulator(client, RADISSON, "N05")  # «замеров не делали»
    assert body["state"] == "answered_unknown" and body["facts"][0]["status"] == "unknown"
    assert body["agent"]["followup"]


def test_text_without_fields_stays_unparsed(client):
    payload = {"feature_id": "M02", "answer_text": "Уточню у коллег.", "is_simulated": True}
    body = client.post(f"/api/hotelier/{RADISSON}/answers", json=payload).json()
    assert body["state"] == "answered_unparsed" and body["facts"] == []


def test_some_rooms_are_not_sent_as_fields():
    q = {"feature_id": "M02", "parsed_fields": list(CAT.get("M02").fields)}
    out = answer_from_text(q, "Да, балкон есть, но не во всех номерах.", gateway=OFFLINE)
    assert out.body == {} and "в каких именно" in out.report["followup"]


def test_validator_rejects_guesses():
    a03 = CAT.get("A03")
    compact = AnswerCompact(
        answer_state="answered",
        quantifier="unknown",
        fields=[
            FieldAnswer(name="lift_id", value="present", unit="", quote="Да, лифт есть"),
            FieldAnswer(name="door_width_mm", value="900", unit="мм", quote="лифт есть"),
            FieldAnswer(name="cabin_width_mm", value="1100", unit="мм", quote="кабина 110 см"),
            FieldAnswer(name="has_lift", value="true", unit="", quote="Да"),
        ],
        conditions="",
        unclear=[],
    )
    parsed = validate(a03, "Да, лифт есть.", compact)
    assert parsed.fields == {"lift_id": "present"}
    reasons = {r["field"]: r["reason"] for r in parsed.rejected}
    assert reasons["door_width_mm"] == "числа нет в цитате"
    assert reasons["cabin_width_mm"] == "цитаты нет в ответе"
    assert "нет в вопросе" in reasons["has_lift"]


def test_units_and_domain():
    parsed = parse_answer("P01", "Можно с собаками. Вес до 500 кг.", gateway=OFFLINE)
    assert parsed.fields == {"species": "dog"}
    assert "вне области" in parsed.rejected[0]["reason"]
    parsed = parse_answer("A02", "Ширина прохода на входе 90 см.", gateway=OFFLINE)
    assert parsed.fields == {"clear_width_mm": 900}


def test_presence_no_beats_type_word():
    parsed = parse_answer("T04", "Своей парковки нет, рядом городская стоянка.", gateway=OFFLINE)
    assert parsed.fields["parking_owner"] == "none"


def test_llm_path_uses_schema_and_same_validation():
    reply = {
        "answer_state": "answered",
        "quantifier": "all",
        "fields": [
            {"name": "hvac_type", "value": "split", "unit": "", "quote": "сплит-системы везде"},
            {"name": "individual_control", "value": "true", "unit": "", "quote": "пульт"},
        ],
        "conditions": "",
        "unclear": [],
    }
    fake = FakeLLM([reply])
    text = "Да, сплит-системы везде, пульт в каждом номере."
    parsed = parse_answer("S04", text, gateway=Gateway([fake]))
    assert parsed.parsed_by == "llm" and fake.calls[0]["schema"] == answer_schema()
    assert parsed.fields == {"hvac_type": "split", "individual_control": True}


def test_simulator_round_trip_offline():
    """Каждый ответ профилей разбирается правилами обратно в те же поля (кроме «vague»)."""
    for hotel_id in (RADISSON, ARCHITECT, COSMOS):
        for fid, entry in load_profile(hotel_id)["answers"].items():
            if "fields" not in entry or entry.get("style") == "vague":
                continue
            text = simulate_answer(hotel_id, fid)["answer_text"]
            parsed = parse_answer(fid, text, gateway=OFFLINE)
            assert entry["fields"].items() <= parsed.fields.items(), (hotel_id, fid, text)


def test_placement_parse_and_mapping():
    rt = "13736470:3502302001O"
    text = simulate_placement(RADISSON, rt)["answer_text"]
    compact, rejected, _ = parse_placement(text, gateway=OFFLINE)
    assert not rejected and compact.guarantee == "all_guaranteed"
    (p,) = compact.placements
    assert (p.side, p.floor_from, p.floor_to, p.building) == ("SW", "3", "8", "главный корпус")
    # Без справочника корпусов и фасадов размещение не отправляется — не угадываем.
    placements, why = to_placements(compact, {}, {})
    assert placements is None and "нет здания" in why[0]
    placements, why = to_placements(compact, {"главный корпус": "way/1"}, {("way/1", "SW"): "f_sw"})
    assert not why and placements["guarantee"] == "all_guaranteed"
    assert [i["floor"] for i in placements["items"]] == [3, 4, 5, 6, 7, 8]
    assert placements["items"][0] == {"building_id": "way/1", "floor": 3, "windows": ["f_sw"]}


def test_questions_copy_matches_v9():
    # По строкам, а не байтам: в git копия с LF, оригинал v9 — с CRLF.
    v9 = Path("research_notes/Расчёт характеристик/hotelier/questions_26.csv")
    copy = QUESTIONS_CSV.read_text("utf-8").splitlines()
    assert copy == v9.read_text("utf-8").splitlines()


def test_varies_is_not_a_hotel_fact():
    """«Зависит от номера или даты» — как «для части номеров»: только текст и уточнение."""
    q = {"feature_id": "M02", "parsed_fields": list(CAT.get("M02").fields)}
    out = answer_from_text(q, "Да, балкон есть, но это зависит от номера.", gateway=OFFLINE)
    assert out.body == {} and out.report["answer_state"] == "varies"
    assert "от чего это зависит" in out.report["followup"]
    reply = {
        "answer_state": "varies",
        "quantifier": "unknown",
        "fields": [
            {"name": "private_balcony", "value": "true", "unit": "", "quote": "балкон есть"}
        ],
        "conditions": "по датам",
        "unclear": [],
    }
    out = answer_from_text(
        q, "Балкон есть, по датам по-разному.", gateway=Gateway([FakeLLM([reply])])
    )
    assert out.body == {} and out.report["fields"]["private_balcony"]["value"] is True
