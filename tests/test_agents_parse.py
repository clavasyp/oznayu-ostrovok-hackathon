import gzip
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from agents.catalog import coerce_value, load_catalog
from agents.llm import Gateway, LLMUnavailable
from agents.llm.fake import FakeLLM
from agents.search import parse_query
from agents.search.compact import CompactAST, compact_schema, is_leftover, to_full_ast
from agents.search.parse import _cache
from agents.search.validate import problems

TODAY = date(2026, 10, 25)
TRACK = "тихий номер с видом на закат недалеко от моря"
CAT = load_catalog()


@pytest.fixture(autouse=True)
def _no_cache():
    _cache.clear()


def compact(*atoms, **kw) -> CompactAST:
    base = dict(
        city="", date_from="", date_to="", adults="", children_ages=[], pets=[],
        context_text=[], atoms=list(atoms), or_groups=[], negated_atoms=[], clarify=[],
    )  # fmt: skip
    return CompactAST.model_validate(base | kw)


def atom(atom_id, text, feature_id="", **kw):
    base = dict(
        atom_id=atom_id, text=text, requiredness="required", provenance="explicit",
        feature_id=feature_id, field="", op="", value="", unit="", params=[], mapping="exact",
        importance="medium",
    )  # fmt: skip
    return base | kw


def test_catalog_matches_v9_facts():
    keys, ids = defaultdict(set), set()
    with gzip.open(Path("app/fixtures/facts.jsonl.gz"), "rt", encoding="utf-8") as f:
        for line in f:
            fact = json.loads(line)
            ids.add(fact["feature_id"])
            if isinstance(fact["value"], dict):
                keys[fact["feature_id"]].update(fact["value"])
    assert len([f for f in CAT.features.values() if f.source != "placement"]) == 80
    assert ids <= set(CAT.features)
    for fid, fields in keys.items():
        default = CAT.get(fid).default_field
        assert default in fields, f"{fid}: поля {default} нет в фактах v9"


def test_compact_schema_fits_claude_limits():
    schema = json.dumps(compact_schema())
    assert "anyOf" not in schema and "oneOf" not in schema  # нет union и nullable

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert set(node["required"]) == set(node["properties"])  # нет необязательных
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(compact_schema())


def test_track_query_offline_matches_track_example():
    out = parse_query(TRACK, "sochi", today=TODAY, gateway=Gateway([]))
    ast = out.ast
    assert ast.parsed_by == "rules" and not ast.unparsed_spans and not out.problems
    assert [(c.feature_id, c.binding, c.quantifier) for c in ast.hard] == [
        ("V05", "same_placement", "all")
    ]
    soft = {c.feature_id: c for c in ast.soft}
    assert set(soft) == {"W01", "N02", "N04"}
    assert soft["W01"].value == 800 and soft["W01"].field == "distance_m"
    assert abs(sum(c.weight for c in ast.soft) - 1) < 0.01
    assert {c.about for c in ast.clarify} == {"тихий", "даты поездки"}
    for c in ast.hard + ast.soft:
        assert TRACK[c.span.start : c.span.end] == c.span.text == c.text_span
    assert problems(ast, CAT) == []


def test_minutes_on_foot_become_meters_by_walking_speed():
    ast = parse_query("до пляжа не больше десяти минут пешком", today=TODAY, mode="rules").ast
    (w01,) = ast.hard
    assert w01.feature_id == "W01" and w01.value == 10 * CAT.walk_speed_m_per_min


def test_requiredness_follows_guest_words():
    text = "Море из окна обязательно. Лучше, чтобы этаж не выше третьего."
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [c.feature_id for c in ast.hard] == ["V01"]
    assert [(c.feature_id, c.op, c.value) for c in ast.soft] == [("floor", "lte", 3)]


def test_nothing_is_lost_without_catalog():
    text = (
        "Приехал посмотреть Москву: нужно быстро доехать до Большого театра. "
        "Хорошо бы рядом итальянский ресторан."
    )
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert ast.city_id == "moscow"
    by_contract = {o.nearest_contracts[0]: o for o in ast.out_of_catalog}
    assert by_contract["trip_to_target"].requiredness == "required"
    assert by_contract["nearby_place"].normalized == "cuisine=italian"
    assert by_contract["nearby_place"].requiredness == "desired"
    assert {s.text for s in ast.unparsed_spans} >= {"Приехал посмотреть"}


def test_partial_required_stays_unconfirmed():
    """«Без лестниц» ≠ «нет известных лестниц»: мягкое условие + обязательное вне каталога."""
    ast = parse_query("до пляжа обязательно без лестниц", today=TODAY, mode="rules").ast
    assert not ast.hard
    assert [c.feature_id for c in ast.soft] == ["W05"]
    (o,) = ast.out_of_catalog
    assert o.requiredness == "required" and o.mapping_status == "partial"
    assert o.nearest_contracts == ["W05"]


def test_to_full_ast_guards_model_output():
    raw = "балкон или терраса обязательно, вид на закат, телепорт"
    c = compact(
        atom("a1", "балкон", "M02", field="private_balcony"),
        atom("a2", "терраса", "M02", field="private_terrace"),
        atom("a3", "вид на закат", "V05", provenance="inferred"),
        atom("a4", "телепорт", "", mapping="unmatched"),
        atom("a5", "бассейн на крыше", "F05"),  # такого фрагмента в запросе нет
        atom("a6", "балкон", "M02", field="no_such_field"),
        or_groups=[{"group_id": "g1", "atom_ids": ["a1", "a2"]}],
    )
    ast = to_full_ast(c, raw, CAT, parsed_by="llm")
    assert [h.atom_id for h in ast.hard] == ["a1", "a2"]
    assert {h.binding for h in ast.hard} == {"same_placement"}
    ops = {n.node_id: n for n in ast.logic}
    assert ops["or_g1"].op == "OR" and ast.hard_root == "root"
    assert {o.atom_id for o in ast.out_of_catalog} == {"a4", "a6"}
    assert {c.about for c in ast.clarify} == {"вид на закат", "бассейн на крыше"}


def test_llm_used_only_when_rules_leave_a_rest():
    text = "Мама ходит с тростью: от улицы до номера обязательно без ступеней."
    reply = compact(
        atom(
            "a1",
            "от улицы до номера обязательно без ступеней",
            "A02",
            field="step_free",
            mapping="partial",
        ),  # fmt: skip
        context_text=["Мама ходит с тростью"],
    ).model_dump()
    fake = FakeLLM([reply])
    out = parse_query(text, today=TODAY, gateway=Gateway([fake]))
    assert out.ast.parsed_by == "llm" and len(fake.calls) == 1
    assert "Мама ходит с тростью" in fake.calls[0]["user"]
    assert fake.calls[0]["schema"] == compact_schema()
    assert out.ast.out_of_catalog[0].requiredness == "required"

    fake_track = FakeLLM([])
    parse_query(TRACK, today=TODAY, gateway=Gateway([fake_track]))
    assert fake_track.calls == []  # правила покрыли всё — модель не зовём


def test_llm_failure_falls_back_to_rules_with_note():
    text = "Мама ходит с тростью, нужен лифт"
    out = parse_query(text, today=TODAY, gateway=Gateway([FakeLLM([LLMUnavailable("нет сети")])]))
    assert out.ast.parsed_by == "rules" and "офлайн" in out.notes[0]
    assert [c.feature_id for c in out.ast.hard] == ["A03"]
    out = parse_query(text, today=TODAY, gateway=Gateway([]))
    assert "ключ LLM не задан" in out.notes[0]


def test_coerce_value_units():
    f = CAT.get("A03").fields["door_width_mm"]
    assert coerce_value(f, "90", "см", 80) == 900
    assert coerce_value(CAT.get("W01").fields["distance_m"], "1,5", "км", 80) == 1500
    with pytest.raises(ValueError):
        coerce_value(CAT.get("B01").fields["surface"], "лава", "", 80)


def test_rules_reference_regression():
    """Правила на эталонах (их писали по этим же запросам — это защита от регрессий, не оценка)."""
    from agents.eval.parse_eval import REFERENCE, evaluate_case, summarize

    cases = [json.loads(line) for line in REFERENCE.read_text("utf-8").splitlines() if line]
    s = summarize([evaluate_case(c, "rules") for c in cases], "rules")
    assert s["cases"] >= 37 and s["cases_with_problems"] == 0
    assert s["hard"]["precision"] >= 0.95 and s["hard"]["recall"] >= 0.9
    assert s["lost_required"] <= 2


def test_api_search_parses_text():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.post(
            "/api/search", json={"text": "Только первый или второй этаж", "city_id": "sochi"}
        ).json()
    assert body["ast"]["parsed_by"] == "rules" and body["ast"]["city_id"] == "sochi"
    assert [(h["feature_id"], h["op"], h["value"]) for h in body["ast"]["hard"]] == [
        ("floor", "in", [1, 2])
    ]
    assert any("офлайн" in n for n in body["notes"])
    # Этаж категорий пока не известен ни у одного отеля → «Нужно уточнить», а не «Подходит».
    assert {r["status"] for r in body["results"]} == {"needs_check"}


def test_compact_schema_file_in_sync():
    path = Path("agents/schemas/query_ast_compact.schema.json")
    assert json.loads(path.read_text("utf-8")) == compact_schema()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("без собак", [("P01", "species", "in", ["cat", "none"])]),
        ("у меня аллергия, только отель без собак", [("P01", "species", "in", ["cat", "none"])]),
        ("не на первом этаже", [("floor", "ordinal", "ne", 1)]),
        (
            "номер с балконом, но не на первом этаже",
            [("M02", "private_balcony", "eq", True), ("floor", "ordinal", "ne", 1)],
        ),
        ("номер без балкона обязательно", [("M02", "private_balcony", "eq", False)]),
        # «без» — часть смысла фразы: условие не переворачивается.
        ("обязательно без пересадок", [("T01", "transfers", "eq", 0)]),
        ("вход без ступеней обязательно", [("A02", "threshold_mm", "lte", 20)]),
    ],
)
def test_negation_flips_or_keeps_meaning(text, expected):
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.field, c.op, c.value) for c in ast.hard] == expected


def test_negation_that_cannot_be_expressed_stays_unparsed():
    # «Не тихий номер» — правилами не выразить: кусок непонятый, но не противоположное условие.
    ast = parse_query("номер не тихий", today=TODAY, mode="rules").ast
    assert not ast.hard and not ast.soft and [s.text for s in ast.unparsed_spans] == ["не тихий"]


def test_not_required_and_dismissed():
    ast = parse_query("кондиционер не обязательно", today=TODAY, mode="rules").ast
    assert not ast.hard and [c.feature_id for c in ast.soft] == ["S04"]
    ast = parse_query("балкон не нужен, но лифт обязательно", today=TODAY, mode="rules").ast
    assert [c.feature_id for c in ast.hard] == ["A03"] and not ast.soft


def test_unknown_next_to_required_is_required_out_of_catalog():
    text = "С собакой 10 кг, обязательно без ковролина. Здравствуйте!"
    ast = parse_query(text, today=TODAY, mode="rules").ast
    (rest,) = [o for o in ast.out_of_catalog if is_leftover(o)]
    assert rest.text_span == "без ковролина" and rest.requiredness == "required"
    assert [s.text for s in ast.unparsed_spans] == [
        "Здравствуйте"
    ]  # без «обязательно» — как раньше


def test_tails_of_understood_phrases_are_covered():
    ast = parse_query("обязательно рядом с аптекой", today=TODAY, mode="rules").ast
    assert [c.feature_id for c in ast.hard] == ["L01"]
    assert not ast.unparsed_spans and not ast.out_of_catalog


def test_required_leftover_keeps_hotel_below_fits():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.post(
            "/api/search", json={"text": "обязательно без ковролина", "city_id": "sochi"}
        ).json()
    assert {r["status"] for r in body["results"]} == {"needs_check"}


def test_cache_keeps_notes_and_returns_copies():
    text = "Мама ходит с тростью, нужен лифт"
    reply = compact(
        atom("a1", "нужен лифт", "A03", field="lift_id", op="ne", value="none"),
    ).model_dump()
    fake = FakeLLM([reply])
    first = parse_query(text, today=TODAY, gateway=Gateway([fake]))
    first.ast.hard.clear()  # вызывающий код поменял свой AST — кэш не должен это увидеть
    again = parse_query(text, today=TODAY, gateway=Gateway([fake]))
    assert len(fake.calls) == 1  # второй раз — из кэша
    assert [c.feature_id for c in again.ast.hard] == ["A03"]
    assert again.notes == first.notes and again.notes  # «Мама ходит с тростью» не разобрано
