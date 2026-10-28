"""Свежесть данных (core/freshness.py): дата значения, срок обновления и чем обновить."""

from datetime import UTC, datetime

from core.facts import Fact
from core.freshness import freshness, summary


def fact(**kw):
    base = {"fact_id": "f", "hotel_id": "1", "city_id": "sochi", "subject_level": "hotel",
            "subject_id": "hotel:1", "feature_id": "A03", "value": {"lift_id": "present"},
            "status": "ostrovok_content", "algorithm_version": "ostrovok-amenities.1",
            "observed_at": datetime(2026, 10, 24, tzinfo=UTC)}  # fmt: skip
    return Fact(**(base | kw))


def test_page_data_is_due_in_a_month_with_its_refresh_step():
    f = freshness(fact(), now=datetime(2026, 10, 28, tzinfo=UTC))
    assert f["date_kind"] == "observed" and f["refresh_due"].startswith("2026-11-23")
    assert not f["stale"] and "build_hotel_amenities" in f["refresh_hint"]
    assert freshness(fact(), now=datetime(2026, 12, 1, tzinfo=UTC))["stale"]


def test_own_refresh_date_and_pack_date_and_hotelier():
    climate = fact(feature_id="C01", status="computed", value={"mean": 11.0},
                   algorithm_version="env.climate_monthly.1",
                   refresh_after="2027-10-24T00:00:00+00:00")  # fmt: skip
    assert freshness(climate)["refresh_due"].startswith("2027-10-24")
    assert "build_environment" in freshness(climate)["refresh_hint"]
    route = fact(feature_id="W01", status="estimate", algorithm_version="route-poi.1",
                 value={"distance_m": 500}, observed_at=None, date_kind="pack_built_at",
                 calculated_at=datetime(2026, 10, 28, tzinfo=UTC))  # fmt: skip
    assert freshness(route)["date_kind"] == "pack_built_at"
    answer = fact(status="hotelier", algorithm_version=None)
    assert "кабинете" in freshness(answer)["refresh_hint"]


def test_summary_without_database():
    unknown = fact(fact_id="g", status="unknown", value=None, unknown_reason="x")
    rows = summary([fact(), unknown], datetime(2027, 1, 1, tzinfo=UTC))
    assert [(r["feature_id"], r["n"], r["stale"]) for r in rows] == [("A03", 1, 1)]


def test_answer_on_stale_fact_is_kept_but_marked():
    """Устаревшее не выбрасывается: «да» остаётся, в объяснении — «данные пора обновить»."""
    from agents.explain import build_claims
    from agents.query_ast import Condition
    from core.search import evaluate

    old = fact(observed_at=datetime(2025, 1, 1, tzinfo=UTC))
    lift = Condition(feature_id="A03", field="lift_id", op="ne", value="none", text_span="лифт")
    item = evaluate([old], lift, hard=True)
    assert item["verdict"] == "yes" and item["stale"] is True
    result = {"hard": [item], "soft": [], "status": "fits", "hotel": {"name": "x"}}
    assert "пора обновить" in " ".join(c.text for c in build_claims(result, lambda _: old))
