from agents.query_ast import Condition
from core.facts import Fact
from core.search import evaluate
from core.verdict import Verdict


def fact(n, value, status="computed", subject="hotel:1", params=None, **kw):
    return Fact(
        fact_id=f"f{n}",
        hotel_id="1",
        city_id="sochi",
        subject_level=subject.split(":")[0],
        subject_id=subject,
        feature_id="N02",
        params=params or {},
        value=value,
        status=status,
        unknown_reason="нет данных" if status == "unknown" else None,
        **kw,
    )


ROAD_FAR = Condition(feature_id="N02", op="gte", value=100, field="distance_m")


def test_all_buildings_yes_is_yes():
    facts = [
        fact(1, {"distance_m": 150}, subject="building:a"),
        fact(2, {"distance_m": 300}, subject="building:b"),
    ]
    assert evaluate(facts, ROAD_FAR, hard=True)["verdict"] == Verdict.yes


def test_all_buildings_no_is_no():
    facts = [
        fact(1, {"distance_m": 10}, subject="building:a"),
        fact(2, {"distance_m": 40}, subject="building:b"),
    ]
    assert evaluate(facts, ROAD_FAR, hard=True)["verdict"] == Verdict.no


def test_buildings_disagree_needs_check():
    # Так у v9: у отеля два корпуса, один у дороги — где окажется гость, неизвестно (D17).
    facts = [
        fact(1, {"distance_m": 10}, subject="building:a"),
        fact(2, {"distance_m": 300}, subject="building:b"),
    ]
    r = evaluate(facts, ROAD_FAR, hard=True)
    assert r["verdict"] == Verdict.unknown
    assert "корпус" in r["reason"]


def test_sources_disagree_on_same_place_is_conflict():
    facts = [
        fact(1, {"distance_m": 10}, subject="building:a"),
        fact(2, {"distance_m": 300}, subject="building:a", status="hotelier"),
    ]
    r = evaluate(facts, ROAD_FAR, hard=True)
    assert r["verdict"] == Verdict.unknown
    assert r["conflict_fact_ids"] == ["f1", "f2"]


def test_estimate_does_not_close_hard_but_counts_in_soft():
    facts = [fact(1, {"distance_m": 300}, status="estimate")]
    assert evaluate(facts, ROAD_FAR, hard=True)["verdict"] == Verdict.unknown
    assert evaluate(facts, ROAD_FAR, hard=False)["verdict"] == Verdict.yes


def test_unknown_is_not_no():
    r = evaluate([fact(1, None, status="unknown")], ROAD_FAR, hard=True)
    assert r["verdict"] == Verdict.unknown and r["reason_code"] == "нет данных"
    assert r["reason"] == "Пока нет данных"  # неизвестный код не ломает ответ


def test_params_select_the_meaning():
    facts = [
        fact(1, {"distance_m": 10}, params={"target": "stop"}),
        fact(2, {"distance_m": 300}, params={"target": "beach"}),
    ]
    to_beach = Condition(
        feature_id="N02", op="gte", value=100, field="distance_m", params={"target": "beach"}
    )
    assert evaluate(facts, to_beach, hard=True)["verdict"] == Verdict.yes


def test_quantifier_any_needs_one_yes():
    facts = [
        fact(1, {"distance_m": 10}, subject="building:a"),
        fact(2, {"distance_m": 300}, subject="building:b"),
    ]
    anywhere = ROAD_FAR.model_copy(update={"quantifier": "any"})
    assert evaluate(facts, anywhere, hard=True)["verdict"] == Verdict.yes


def test_present_without_type_is_unknown_not_no():
    # «Есть, тип не назван» (D39): на «сплит-система» — уточнить, а не «нет».
    present = [fact(1, {"hvac_type": "present"})]
    split = Condition(feature_id="S04", op="eq", value="split", field="hvac_type")
    r = evaluate(present, split, hard=True)
    assert r["verdict"] == Verdict.unknown and r["reason_code"] == "value_unspecified"
    types = split.model_copy(update={"op": "in", "value": ["split", "central"]})
    assert evaluate(present, types, hard=True)["verdict"] == Verdict.unknown
    any_hvac = split.model_copy(update={"op": "ne", "value": "none"})
    assert evaluate(present, any_hvac, hard=True)["verdict"] == Verdict.yes
    exists = split.model_copy(update={"op": "exists", "value": None})
    assert evaluate(present, exists, hard=True)["verdict"] == Verdict.yes
    absent = [fact(1, {"hvac_type": "none"})]
    for c in (split, any_hvac, exists):
        assert evaluate(absent, c, hard=True)["verdict"] == Verdict.no
