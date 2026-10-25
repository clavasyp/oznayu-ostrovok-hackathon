from fastapi.testclient import TestClient

from agents.explain import build_claims, explain
from agents.explain.explain import assemble, problems
from agents.llm import Gateway, LLMUnavailable
from agents.llm.fake import FakeLLM
from app.main import create_app
from core.facts import Fact

RADISSON = "13736470"
W01 = Fact(
    fact_id="f_w01",
    hotel_id="h",
    city_id="sochi",
    subject_level="hotel",
    subject_id="hotel:h",
    feature_id="W01",
    value={"distance_m": 155.3},
    status="estimate",
)
FACTS = {W01.fact_id: W01}


def sunset_result():
    """Закат виден только с 5-го этажа: размещения 2–4 «нет», 5–8 «да» (D17)."""
    placements = [
        {
            "placement_id": f"p{n}",
            "building_id": "b",
            "floor": n,
            "verdict": "yes" if n >= 5 else "no",
        }
        for n in range(2, 9)
    ]
    v05 = {
        "feature_id": "V05",
        "atom_id": "a1",
        "text_span": "с видом на закат",
        "verdict": "unknown",
        "fact_ids": ["v1", "v2"],
        "reason_code": "depends_on_room",
        "reason": "Зависит от номера.",
    }
    w01 = {
        "feature_id": "W01",
        "atom_id": "a2",
        "text_span": "недалеко от моря",
        "verdict": "yes",
        "fact_ids": ["f_w01"],
        "weight": 1.0,
    }
    return {
        "status": "needs_check",
        "status_label": "Нужно уточнить",
        "hard": [v05],
        "soft": [w01],
        "categories": [
            {
                "room_type_id": "h:sea",
                "name": "С видом на море",
                "conditions": [v05],
                "placements": placements,
            },
        ],  # fmt: skip
        "best_category": "h:sea",
    }


def test_sunset_only_from_fifth_floor():
    claims = build_claims(sunset_result(), FACTS.get)
    texts = [c.text for c in claims]
    assert "закат из окна: только с 5-го этажа в категории «С видом на море»" in texts
    assert "пешком до пляжа: да (155 м, оценка)" in texts  # цифра — из факта, с паспортом


def test_template_says_blockers_first_then_but():
    e = explain(sunset_result(), FACTS.get)
    assert e.by == "template"
    assert e.text.startswith("Закат из окна: нужно уточнить — зависит от номера")
    assert ", но пешком до пляжа: да (155 м, оценка)." in e.text


def test_llm_orders_claims_and_links_only():
    reply = {"order": ["c2", "c1", "c3"], "links": ["", "но", "при этом"]}
    fake = FakeLLM([reply])
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "llm"
    assert e.text == (
        "Закат из окна: только с 5-го этажа в категории «С видом на море», но закат из окна: "
        "нужно уточнить — зависит от номера, при этом пешком до пляжа: да (155 м, оценка)."
    )
    assert "Утверждения (данные)" in fake.calls[0]["user"]


def test_llm_order_that_drops_a_blocker_falls_back_to_template():
    fake = FakeLLM([{"order": ["c3"], "links": [""]}])  # про закат промолчали
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "template" and "пропущены обязательные" in e.notes[0]
    fake = FakeLLM([{"order": ["c9"], "links": [""]}])
    e = explain(sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([fake]))
    assert e.by == "template" and "чужие ID" in e.notes[0]
    e = explain(
        sunset_result(), FACTS.get, use_llm=True, gateway=Gateway([FakeLLM([LLMUnavailable("x")])])
    )
    assert e.by == "template" and "модель недоступна" in e.notes[0]


def test_numbers_must_come_from_claims():
    claims = build_claims(sunset_result(), FACTS.get)
    order = [c.claim_id for c in claims]
    text = assemble(claims, order, [""] * len(order)) + " До моря 50 м."
    assert any("числа не из фактов: 50" in p for p in problems(claims, order, [], text))


def test_api_search_has_template_explanation_and_explain_endpoint():
    with TestClient(create_app()) as c:
        body = c.post("/api/search", json={"text": "недалеко от моря", "city_id": "sochi"}).json()
        first = body["results"][0]
        assert first["explanation"]["by"] == "template" and first["explanation"]["text"]
        one = c.post(
            "/api/explain",
            json={"text": "тихий номер с видом на закат", "city_id": "sochi", "hotel_id": RADISSON},
        ).json()
        assert one["hotel_id"] == RADISSON and one["explanation"]["by"] == "template"
        assert any("ключ LLM не задан" in n for n in one["notes"])  # в тестах ключей нет
        missing = c.post("/api/explain", json={"text": "у моря", "hotel_id": "nope"})
        assert missing.status_code == 404
