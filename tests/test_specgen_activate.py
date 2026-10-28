"""Новая характеристика текстом — до конца (задача 4): черновик → проверка → пробный расчёт →
включение (расчёт по всем отелям, факты в хранилище, запись в каталоге) → разбор узнаёт слова
гостя → поиск сравнивает по фактам. Места карт отмечены не все: число мест — «не меньше»."""

import pytest

from agents.catalog import catalog as catalog_module
from agents.catalog import load_catalog
from agents.search import parse_query
from agents.specgen import check, draft_spec, trial_run
from agents.specgen.activate import activate
from app.stub_store import StubStore
from core.search import search
from core.verdict import GuestStatus

HOTELS = {
    h: {"hotel_id": h, "city_id": "sochi", "name": h, "latitude": 43.58, "longitude": 39.72}
    for h in ("cafe", "none")
}


@pytest.fixture(autouse=True)
def clean_catalog(monkeypatch):
    """Включённые характеристики живут в каталоге процесса — у каждого теста свои."""
    monkeypatch.setattr(catalog_module, "_RUNTIME", {})
    load_catalog.cache_clear()
    yield
    load_catalog.cache_clear()


def store():
    near = {"cafe": {"place_id": "osm:node/1", "name": "Кофейня", "distance_m": 120}}
    return StubStore(hotel_by_id=HOTELS, places_near={"coffee": near})


def test_text_to_searchable_feature():
    d = draft_spec("сколько кафе в 500 м", use_llm=False)
    assert d.spec.id == "coffee_count_500m" and d.trial_ready and not d.problems
    s = store()
    tried = trial_run(d.spec, s, "sochi")
    assert tried["ran"] and tried["coverage"] == 1.0

    result = activate(d.spec, s, ("sochi",))
    assert result["activated"] and result["facts"] == 2
    assert result["cities"]["sochi"]["coverage"] == 1.0
    assert [f["id"] for f in s.feature_specs()] == ["coffee_count_500m"]
    feature = load_catalog().get("coffee_count_500m")
    assert feature is not None and feature.runtime

    ast = parse_query("хочу отель, где много кафе", "sochi", mode="rules").ast
    (cond,) = ast.soft
    assert (cond.feature_id, cond.op, cond.value) == ("coffee_count_500m", "gte", 3)

    ast = parse_query("обязательно много кафе", "sochi", mode="rules").ast
    by_hotel = {r["hotel"]["hotel_id"]: r for r in search(s, ast)}
    # По картам одно кафе, а нужно «много» — но отмечены не все места: не «нет», а «уточним»
    assert by_hotel["cafe"]["hard"][0]["reason_code"] == "count_lower_bound"
    assert {r["status"] for r in by_hotel.values()} == {GuestStatus.needs_check}


def test_place_count_allows_only_at_least():
    spec = draft_spec("сколько кафе в 500 м", use_llm=False).spec
    lte = spec.model_copy(
        update={"search": spec.search.model_copy(update={"operators": ["gte", "lte"]})}
    )
    assert any("только gte" in p for p in check(lte).problems)
    assert not activate(lte, store(), ("sochi",))["activated"]
