"""Ядро после полного каталога Островка (№26, D41): ответ отеля для всех номеров, причины
«неизвестно» по данным Островка, страница выдачи и свёрнутые факты окон в карточке."""

from fastapi.testclient import TestClient

from agents.query_ast import Condition, QueryAST
from app.main import create_app
from app.stub_store import StubStore
from core.facts import Fact
from core.search import evaluate, search
from core.verdict import GuestStatus

HOTEL = {"hotel_id": "h1", "city_id": "sochi", "name": "Тест"}
AC = Condition(feature_id="S04", op="eq", value=True)


def fact(fid, subject, level, status, value, eligible=None):
    return Fact(
        fact_id=fid,
        hotel_id="h1",
        city_id="sochi",
        subject_level=level,
        subject_id=subject,
        feature_id="S04",
        value=value,
        status=status,
        unknown_reason="ostrovok_field_unknown_or_missing" if value is None else None,
        hard_condition_eligible=eligible,
    )


def run(facts):
    rts = [{"room_type_id": "h1:a", "name": "A"}, {"room_type_id": "h1:b", "name": "B"}]
    s = StubStore(hotel_by_id={"h1": HOTEL}, by_hotel={"h1": {"S04": facts}},
                  room_types_by_hotel={"h1": rts})  # fmt: skip
    return search(s, QueryAST(raw_text="кондиционер", city_id="sochi", hard=[AC]))[0]


def test_hotel_answer_applies_to_every_category():
    # У категорий — заглушки Островка «неизвестно», отель ответил про все номера.
    facts = [
        fact("a", "h1:a", "room_type", "unknown", None),
        fact("b", "h1:b", "room_type", "unknown", None),
        fact("h", "hotel:h1", "hotel", "simulated_hotelier", True),
    ]
    assert run(facts)["status"] == GuestStatus.fits


def test_hotel_answer_against_category_fact_is_a_conflict():
    facts = [fact("a", "h1:a", "room_type", "ostrovok_content", False),
             fact("h", "hotel:h1", "hotel", "hotelier", True)]  # fmt: skip
    result = evaluate([facts[0], facts[1].model_copy(update={"subject_id": "h1:a"})], AC, True)
    assert result["reason_code"] == "conflict"


def test_ostrovok_statement_not_for_every_room_is_not_an_estimate():
    f = fact("a", "h1:a", "room_type", "ostrovok_content", True, eligible=False)
    assert evaluate([f], AC, hard=True)["reason_code"] == "ostrovok_partial"


def test_specific_answer_refines_unnamed_type():
    """«Питомцы разрешены, вид не назван» (Островок) и ответ «с собакой можно» — одно место:
    конкретный ответ уточняет, а не спорит; без него — «тип не назван» (D39)."""
    dog = Condition(feature_id="P01", op="eq", value="dog", field="species")
    page = fact("p", "hotel:h1", "hotel", "ostrovok_content", {"species": "present"})
    answer = fact("s", "hotel:h1", "hotel", "simulated_hotelier", {"species": "dog"})
    page, answer = (f.model_copy(update={"feature_id": "P01"}) for f in (page, answer))
    assert evaluate([page], dog, hard=False)["reason_code"] == "value_unspecified"
    assert evaluate([page, answer], dog, hard=False)["verdict"] == "yes"
    no_dogs = answer.model_copy(update={"value": {"species": "cat"}})
    assert evaluate([page, no_dogs], dog, hard=False)["verdict"] == "no"


def test_needs_check_ostrovok_yes_ranks_above_no_data():
    """Внутри «Нужно уточнить»: Островок «да» не для всех номеров — выше, чем «нет данных»,
    и выше, чем Островок «нет»."""
    rts = [{"room_type_id": "x:a", "name": "A"}]
    hotels = {h: {"hotel_id": h, "city_id": "sochi", "name": n}
              for h, n in [("x1", "А нет данных"), ("x2", "Б нет"), ("x3", "В да")]}  # fmt: skip

    def partial(h, value):
        f = fact(f"f{h}", "x:a", "room_type", "ostrovok_content", value, eligible=False)
        return [f.model_copy(update={"hotel_id": h})]

    s = StubStore(
        hotel_by_id=hotels,
        by_hotel={"x1": {"S04": []}, "x2": {"S04": partial("x2", False)},
                  "x3": {"S04": partial("x3", True)}},
        room_types_by_hotel={h: rts for h in hotels},
    )  # fmt: skip
    results = search(s, QueryAST(raw_text="кондиционер", city_id="sochi", hard=[AC]))
    assert [r["hotel"]["hotel_id"] for r in results] == ["x3", "x1", "x2"]
    assert {r["status"] for r in results} == {GuestStatus.needs_check}


def test_search_page_and_counts():
    with TestClient(create_app()) as c:
        full = c.post("/api/search", json={"text": "с собакой", "city_id": "sochi"}).json()
        page = c.post(
            "/api/search", json={"text": "с собакой", "city_id": "sochi", "limit": 2, "offset": 1}
        ).json()
    assert page["total"] == full["total"] == len(full["results"]) > 2
    assert sum(page["counts"].values()) == page["total"] and len(page["results"]) == 2
    ids = [r["hotel"]["hotel_id"] for r in full["results"]]
    assert [r["hotel"]["hotel_id"] for r in page["results"]] == ids[1:3]


def test_hotel_card_folds_window_facts():
    with TestClient(create_app()) as c:
        body = c.get("/api/hotels/6496437").json()  # «Грейс Эль Параисо»: тысячи фактов окон
    levels = {f["subject_level"] for f in body["facts"].get("V05", [])}
    assert levels <= {"hotel"} and body["windows"]["V05"]["facts"] > 100  # окна — счётчиками
    assert body["windows"]["V05"]["computed_yes"] > 0
