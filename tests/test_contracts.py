import gzip
import json
from pathlib import Path

import pytest

from agents.query_ast import SCHEMA_PATH, QueryAST, json_schema
from app.stub_store import StubStore
from core.facts import Fact, Status, make_fact_id
from core.verdict import GuestStatus, Verdict, guest_status


def base_fact(**over):
    data = {
        "fact_id": "x" * 24,
        "hotel_id": "1",
        "city_id": "sochi",
        "subject_level": "hotel",
        "subject_id": "hotel:1",
        "feature_id": "N04",
        "status": "computed",
        "value": {"count": 1},
    }
    data.update(over)
    return data


def test_every_v9_fact_fits_contract():
    path = Path("research_notes/Расчёт характеристик/integrated/all8_facts.jsonl.gz")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        n = sum(1 for line in f if Fact.model_validate_json(line))
    assert n == 35015


def test_all_v9_fixture_facts_fit_contract():
    store = StubStore.load()
    assert len(store.facts) > 1000
    assert {f.status for f in store.facts.values()} >= {
        Status.computed,
        Status.estimate,
        Status.unknown,
    }


def test_unknown_needs_reason():
    with pytest.raises(ValueError):
        Fact.model_validate(base_fact(status="unknown", value=None))
    Fact.model_validate(base_fact(status="unknown", value=None, unknown_reason="no_data"))


def test_valued_status_needs_value():
    with pytest.raises(ValueError):
        Fact.model_validate(base_fact(value=None))


def test_hard_eligibility():
    assert Fact.model_validate(base_fact()).usable_in_hard
    assert not Fact.model_validate(base_fact(status="estimate")).usable_in_hard  # D18
    assert not Fact.model_validate(base_fact(hard_condition_eligible=False)).usable_in_hard
    assert Fact.model_validate(base_fact(status="ostrovok_content")).usable_in_hard  # D26


def test_v9_extra_fields_survive():
    f = Fact.model_validate(base_fact(component_diagnostics={"a": 1}))
    assert f.model_dump()["component_diagnostics"] == {"a": 1}


def test_fact_id_is_deterministic():
    a = make_fact_id("1", "hotel:1", "E03", {"radius_m": 300}, "v1")
    assert a == make_fact_id("1", "hotel:1", "E03", {"radius_m": 300}, "v1")
    assert a != make_fact_id("1", "hotel:1", "E03", {"radius_m": 301}, "v1")
    assert len(a) == 24


def test_guest_status():
    assert guest_status([Verdict.yes, Verdict.yes]) == GuestStatus.fits
    assert guest_status([Verdict.yes, Verdict.unknown]) == GuestStatus.needs_check
    assert guest_status([Verdict.unknown, Verdict.no]) == GuestStatus.not_fit
    assert guest_status([]) == GuestStatus.fits


def test_query_ast_example_and_schema_file_in_sync():
    example = json.loads((SCHEMA_PATH.parent / "examples" / "track_query.json").read_text("utf-8"))
    QueryAST.model_validate(example)
    assert json.loads(SCHEMA_PATH.read_text("utf-8")) == json_schema(), (
        "схема устарела: python -m agents.query_ast"
    )


def test_query_ast_rejects_unknown_fields():
    with pytest.raises(ValueError):
        QueryAST.model_validate(
            {"raw_text": "x", "hard": [{"feature_id": "W01", "op": "lte", "x": 1}]}
        )
