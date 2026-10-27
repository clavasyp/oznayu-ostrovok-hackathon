"""Размещения категорий (D17): условия уровня окна — все вместе на одном размещении."""

from agents.query_ast import Condition, QueryAST
from app.stub_store import StubStore
from core.facts import Fact
from core.search import search, window_subject
from core.verdict import GuestStatus, Verdict

HOTEL = {"hotel_id": "h1", "city_id": "sochi", "name": "Тест"}
SUNSET = Condition(feature_id="V05", op="eq", value=True)
QUIET = Condition(feature_id="N01", op="lte", value=45, field="lnight_db")
n = 0


def window_fact(feature, building, facade, floor, value, status="computed"):
    global n
    n += 1
    return Fact(
        fact_id=f"w{n}",
        hotel_id="h1",
        city_id="sochi",
        subject_level="facade_physical_floor",
        subject_id=window_subject(building, facade, floor),
        feature_id=feature,
        value=value,
        status=status,
    )


def room_type(rt_id, placements, completeness="complete", guarantee="all_guaranteed"):
    return {
        "room_type_id": rt_id,
        "name": rt_id,
        "placement_set": {
            "completeness": completeness,
            "guarantee": guarantee,
            "status": "simulated_hotelier",
            "source": {},
        },
        "placements": [
            {"placement_id": f"{rt_id}|{b}|{f}", "building_id": b, "floor": f, "windows": w}
            for b, f, w in placements
        ],
    }


def store(facts, room_types):
    by_feature = {}
    for f in facts:
        by_feature.setdefault(f.feature_id, []).append(f)
    return StubStore(
        hotel_by_id={"h1": HOTEL},
        by_hotel={"h1": by_feature},
        room_types_by_hotel={"h1": room_types} if room_types is not None else {},
    )


def run(s, *hard):
    r = search(s, QueryAST(raw_text="тест", city_id="sochi", hard=list(hard)))[0]
    return r


def test_all_placements_yes_fits():
    facts = [
        window_fact("V05", "b", "west", 5, True),
        window_fact("V05", "b", "west", 6, True),
    ]
    r = run(store(facts, [room_type("sea", [("b", 5, ["west"]), ("b", 6, ["west"])])]), SUNSET)
    assert r["status"] == GuestStatus.fits and r["best_category"] == "sea"


def test_conditions_must_hold_on_the_same_placement():
    # 5-й этаж: закат виден, но шумно; 2-й: тихо, но заката нет. По отдельности всё где-то
    # выполняется, вместе — нигде. Значит «не подходит», а не «подходит».
    facts = [
        window_fact("V05", "b", "west", 5, True),
        window_fact("N01", "b", "west", 5, {"lnight_db": 55}),
        window_fact("V05", "b", "west", 2, False),
        window_fact("N01", "b", "west", 2, {"lnight_db": 40}),
    ]
    r = run(
        store(facts, [room_type("std", [("b", 5, ["west"]), ("b", 2, ["west"])])]), SUNSET, QUIET
    )
    assert r["status"] == GuestStatus.not_fit
    assert r["categories"][0]["reason_code"] == "no_single_placement"


def test_some_placements_yes_needs_check():
    facts = [window_fact("V05", "b", "west", 5, True), window_fact("V05", "b", "east", 5, False)]
    rt = room_type("std", [("b", 5, ["west"]), ("b", 5, ["east"])])
    r = run(store(facts, [rt]), SUNSET)
    assert r["status"] == GuestStatus.needs_check
    assert r["categories"][0]["reason_code"] == "depends_on_room"
    weak = room_type("std", [("b", 5, ["west"]), ("b", 5, ["east"])], guarantee="some_possible")
    assert run(store(facts, [weak]), SUNSET)["categories"][0]["reason_code"] == "not_guaranteed"


def test_incomplete_or_missing_placements_need_check():
    facts = [window_fact("V05", "b", "west", 5, True)]
    partial = room_type("std", [("b", 5, ["west"])], completeness="partial")
    r = run(store(facts, [partial]), SUNSET)
    assert r["status"] == GuestStatus.needs_check
    assert r["hard"][0]["reason_code"] == "placements_incomplete"
    r = run(store(facts, []), SUNSET)  # окна посчитаны, но категории не привязаны к ним
    assert r["status"] == GuestStatus.needs_check
    assert r["hard"][0]["reason_code"] == "window_all"  # «да» во всех окнах, но где номер — ?
    assert r["hard"][0]["windows"]["yes"] == {"?": [5]}


def test_corner_room_view_needs_one_window_noise_needs_all():
    facts = [
        window_fact("V05", "b", "west", 5, True),
        window_fact("V05", "b", "north", 5, False),
        window_fact("N01", "b", "west", 5, {"lnight_db": 40}),
        window_fact("N01", "b", "north", 5, {"lnight_db": 60}),
    ]
    corner = [room_type("corner", [("b", 5, ["north", "west"])])]
    assert run(store(facts, corner), SUNSET)["status"] == GuestStatus.fits
    assert run(store(facts, corner), QUIET)["status"] == GuestStatus.not_fit


def test_best_category_wins_and_hotel_condition_applies_to_all():
    facts = [window_fact("V05", "b", "west", 5, True), window_fact("V05", "b", "east", 3, False)]
    rts = [room_type("east", [("b", 3, ["east"])]), room_type("west", [("b", 5, ["west"])])]
    r = run(store(facts, rts), SUNSET)
    assert r["status"] == GuestStatus.fits and r["best_category"] == "west"
    assert {c["room_type_id"]: c["verdict"] for c in r["categories"]} == {
        "west": Verdict.yes,
        "east": Verdict.no,
    }


def category_fact(rt_id, feature, value):
    global n
    n += 1
    return Fact(
        fact_id=f"c{n}",
        hotel_id="h1",
        city_id="sochi",
        subject_level="room_type",
        subject_id=rt_id,
        feature_id=feature,
        value=value,
        status="ostrovok_content",
    )


def test_category_level_fact_is_checked_per_category():
    balcony = Condition(feature_id="M02", op="eq", value=True)
    facts = [category_fact("std", "M02", False), category_fact("lux", "M02", True)]
    rts = [room_type("std", []), room_type("lux", [])]
    r = run(store(facts, rts), balcony)
    assert r["status"] == GuestStatus.fits and r["best_category"] == "lux"
