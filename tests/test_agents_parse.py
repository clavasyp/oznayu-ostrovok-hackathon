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
    assert by_contract["nearby_place"].normalized == "nearby_place:italian"
    assert by_contract["nearby_place"].place.radius_m == 800  # «рядом» — политика словаря мест
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


@pytest.mark.parametrize(
    ("text", "op"),
    [
        ("мало баров рядом", "lte"),
        ("хочу, чтобы баров рядом было мало", "lte"),
        ("немного баров рядом", "lte"),
        ("рядом много баров", "gte"),
    ],
)
def test_bars_few_or_many(text, op):
    """«Мало баров рядом» — не больше двух, а не «бары нужны» (раньше было наоборот)."""
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.op) for c in ast.soft] == [("N04", op)] and not ast.unparsed_spans


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


REMOTE = (
    "Еду в Москву на удалёнку: нужен рабочий стол и Wi-Fi от 50 мегабит. "
    "Хорошо бы потише вокруг и поменьше баров рядом."
)
# Как ответила модель в живом прогоне 27.10: «поменьше баров» — вне каталога, «потише» — N01.
REMOTE_REPLY = compact(
    atom("a1", "на удалёнку", value="удалённая работа", mapping="unmatched",
         requiredness="unspecified"),
    atom("a2", "рабочий стол", "R03", field="desk_clear_width_mm", op="gte", value="600",
         unit="мм"),
    atom("a3", "Wi-Fi от 50 мегабит", "R01", field="download_Mbps", op="gte", value="50",
         unit="мбит"),
    atom("a4", "потише вокруг", "N01", field="lnight_dba", op="lte", value="45", unit="дБ",
         requiredness="desired"),
    atom("a5", "поменьше баров рядом", value="мало баров", mapping="unmatched",
         requiredness="desired"),
    city="moscow",
    context_text=["Еду в Москву"],
).model_dump()  # fmt: skip


def test_auto_keeps_rules_and_takes_only_the_rest_from_model():
    """Сценарий «Удалёнка»: правила не поняли «Еду», «на удалёнку» — модель зовём, но понятое
    правилами («поменьше баров рядом» → N04) она не переписывает."""
    fake = FakeLLM([REMOTE_REPLY])
    ast = parse_query(REMOTE, today=TODAY, gateway=Gateway([fake])).ast
    assert len(fake.calls) == 1 and ast.parsed_by == "llm"
    soft = [(c.feature_id, c.op, c.value, c.span.text) for c in ast.soft]
    assert ("N04", "lte", 2, "поменьше баров рядом") in soft
    assert "N01" not in {c.feature_id for c in ast.soft}  # «потише» уже поняли правила
    assert [(c.feature_id, c.value) for c in ast.hard] == [("R03", 600), ("R01", 50)]
    assert [o.text_span for o in ast.out_of_catalog] == ["на удалёнку"]  # остаток — от модели
    assert not ast.unparsed_spans

    # Режим llm — модель целиком, как в оценке; кэш режимы не смешивает.
    fake_llm = FakeLLM([REMOTE_REPLY])
    ast = parse_query(REMOTE, today=TODAY, gateway=Gateway([fake_llm]), mode="llm").ast
    assert len(fake_llm.calls) == 1 and "N04" not in {c.feature_id for c in ast.soft}


def auto(text, reply):
    _cache.clear()  # тот же запрос с другим ответом модели
    fake = FakeLLM([reply.model_dump()])
    ast = parse_query(text, today=TODAY, gateway=Gateway([fake])).ast
    assert len(fake.calls) == 1 and ast.parsed_by == "llm"
    return ast


def or_groups(ast):
    """ИЛИ в графе обязательных: множества характеристик вариантов."""
    nodes = {n.node_id: n for n in ast.logic}
    by_atom = {c.atom_id: c.feature_id for c in ast.hard}
    return [
        {by_atom[nodes[child].atom_id] for child in n.children} for n in ast.logic if n.op == "OR"
    ]


@pytest.mark.parametrize(
    ("text", "reply", "variants"),
    [
        (  # вариант правил + вариант модели
            "Аптека или супермаркет рядом обязательно.",
            compact(
                atom("a1", "Аптека", "L01", field="distance_m", op="lte", value="1000", unit="м"),
                atom("a2", "супермаркет рядом", "L03", field="distance_m", op="lte",
                     value="1000", unit="м"),
                or_groups=[{"group_id": "g1", "atom_ids": ["a1", "a2"]}],
            ),
            {"L01", "L03"},
        ),
        (
            "Нужна кухня или хотя бы чайник в номере.",
            compact(
                atom("a1", "кухня", "M03", field="private_kitchenette", value="true"),
                atom("a2", "чайник в номере", "M04", field="in_room_kettle", value="true"),
                or_groups=[{"group_id": "g1", "atom_ids": ["a1", "a2"]}],
            ),
            {"M03", "M04"},
        ),
        (  # оба варианта — атомы правил, модель зовут из-за «Еду»
            "Еду в Сочи: нужен балкон или терраса.",
            compact(
                atom("a1", "балкон", "M02", field="private_balcony", value="true"),
                atom("a2", "терраса", "M02", field="private_terrace", value="true"),
                or_groups=[{"group_id": "g1", "atom_ids": ["a1", "a2"]}],
                city="sochi", context_text=["Еду в Сочи"],
            ),
            {"M02"},
        ),
    ],
)  # fmt: skip
def test_auto_keeps_model_or_over_rule_atoms(text, reply, variants):
    """Ревью №29: атом модели на куске правил выбрасывался вместе со своим ИЛИ — гость
    соглашался на любой вариант, а обязательными становились оба (отель только с аптекой —
    «Не подходит»). Теперь атом модели — это тот же атом правил, и ИЛИ переносится на него."""
    ast = auto(text, reply)
    assert or_groups(ast) == [variants] and len(ast.hard) == 2
    assert ast.hard_root == "root" and not ast.out_of_catalog and not ast.unparsed_spans


def test_rules_or_between_matches():
    """Ревью №29: «балкон или терраса» без остатка до модели не доходит, а правила делали из
    «или» «и» — отель только с балконом получал «Не подходит»."""

    def parsed(text):
        return parse_query(text, "sochi", today=TODAY, mode="rules").ast

    for text, variants in (
        ("Нужен балкон или терраса.", {"M02"}),
        ("Номер с балконом или с террасой обязательно.", {"M02"}),
        ("Лифт или пандус обязательно.", {"A03", "A02"}),
        # Два значения одной характеристики: отель, куда пускают только кошек, — не «Не подходит».
        ("С собакой или с кошкой.", {"P01"}),
    ):
        ast = parsed(text)
        assert or_groups(ast) == [variants] and len(ast.hard) == 2, text
    # «Или» между желательными — ИЛИ не нужно; обязательное рядом — само по себе.
    ast = parsed("Хотелось бы балкон или терраса, но обязательно лифт.")
    assert [c.feature_id for c in ast.hard] == ["A03"] and not ast.logic
    # Второй вариант правила не поняли или условие составное — не «И» из половины: вся часть
    # непонятая, у «обязательно» — обязательная вне каталога (D38).
    for text, rest in (
        ("Аптека или супермаркет рядом обязательно.", "Аптека или супермаркет рядом"),
        ("Нужна кухня или хотя бы чайник в номере.", "кухня или хотя бы чайник"),
        ("Обязательно балкон или большая терраса.", "балкон или большая терраса"),
        ("Обязательно с собакой 10 кг или с кошкой.", "с собакой 10 кг или с кошкой"),
    ):
        ast = parsed(text)
        assert not ast.hard and not ast.soft, text
        assert [(o.text_span, o.requiredness) for o in ast.out_of_catalog] == [
            (rest, "required")
        ], text


def test_auto_keeps_model_negation_over_rule_atom():
    """Ревью №29: отрицание не вплотную («собак … быть не должно») правила не видят и дают
    «можно с собакой»; НЕ модели над этим атомом терялось, и аллергику был обязателен отель,
    куда пускают собак. Теперь модель прямо отрицает атом правил — берётся её атом со своим НЕ."""
    text = "Собак в отеле быть не должно, у меня аллергия."
    reply = compact(
        atom("a1", "Собак в отеле быть не должно", "P01", field="species", op="in",
             value="dog,dog_cat,any"),
        context_text=["у меня аллергия"],
        negated_atoms=["a1"],
    )  # fmt: skip
    ast = auto(text, reply)
    (p01,) = ast.hard
    assert (p01.feature_id, p01.value, p01.text_span) == (
        "P01",
        ["dog", "dog_cat", "any"],
        "Собак в отеле быть не должно",
    )
    nodes = {n.node_id: n for n in ast.logic}
    assert nodes[nodes["root"].children[0]].op == "NOT"
    # То же отрицание значением: «пускают только с кошкой или без животных».
    reply = compact(
        atom("a1", "Собак в отеле быть не должно", "P01", field="species", op="in",
             value="cat,none"),
        context_text=["у меня аллергия"],
    )  # fmt: skip
    ast = auto(text, reply)
    assert [(c.feature_id, c.value) for c in ast.hard] == [("P01", ["cat", "none"])]
    assert not ast.logic
    # Правила уже перевернули «без собак», «никаких собак» — НЕ модели над тем же смыслом
    # второй раз не переворачивает: двойного отрицания нет.
    for text, span in (
        ("У меня аллергия, только отель без собак.", "без собак"),
        ("У меня аллергия: никаких собак.", "никаких собак"),
    ):
        reply = compact(
            atom("a1", span, "P01", field="species", op="in", value="dog,dog_cat,any"),
            context_text=["У меня аллергия"],
            negated_atoms=["a1"],
        )
        ast = auto(text, reply)
        assert [(c.feature_id, c.value) for c in ast.hard] == [("P01", ["cat", "none"])], text
        assert not ast.logic


@pytest.mark.parametrize(
    ("text", "reply", "feature"),
    [
        (
            "Лифт — лишнее, мы налегке.",
            compact(
                atom("a1", "Лифт — лишнее", "A03", field="lift_id", op="ne", value="none",
                     requiredness="unspecified"),
                context_text=["мы налегке"], negated_atoms=["a1"],
            ),
            "A03",
        ),
        (
            "Лифт нам ни к чему.",
            compact(atom("a1", "Лифт нам ни к чему", "A03", op="eq", value="none",
                         requiredness="unspecified")),
            "A03",
        ),
        (
            "Можно и без балкона, главное кухня в номере.",
            compact(
                atom("a1", "Можно и без балкона", "M02", field="private_balcony", value="true",
                     requiredness="desired"),
                atom("a2", "кухня в номере", "M03", field="private_kitchenette", value="true"),
            ),
            "M02",
        ),
    ],
)  # fmt: skip
def test_auto_model_negating_rule_atom_removes_it(text, reply, feature):
    """D41: модель не меняет понятое правилами, кроме прямого отрицания на том же куске —
    тогда атом правил снимается. Ревью №29: с ключом «лифт нам ни к чему» и «можно и без
    балкона» оставались обязательными, и отели без лифта или с балконом — «Не подходит»."""
    fake = FakeLLM([reply.model_dump()])
    ast = parse_query(text, today=TODAY, gateway=Gateway([fake])).ast
    assert feature not in {c.feature_id for c in ast.hard}
    # Требования правил («нужен лифт», «без балкона») нет нигде.
    rules = parse_query(text, today=TODAY, mode="rules").ast
    said = {
        (c.feature_id, c.field, c.op, str(c.value)) for c in rules.hard if c.feature_id == feature
    }
    got = {(c.feature_id, c.field, c.op, str(c.value)) for c in ast.hard + ast.soft}
    assert not said & got


def test_auto_never_takes_half_an_or_group():
    """Ревью №29: половина ИЛИ модели не становится отдельным условием. Вариант, который не
    перенести (на куске правил — другая характеристика), гасит всю группу: «кухня или чайник»
    остаётся непонятой и показана гостю, а не становится M03 через И."""
    text = "Нужен лифт, а ещё кухня или чайник в номере."
    reply = compact(
        atom("a1", "лифт", "A03", field="lift_id", op="ne", value="none"),
        atom("a2", "кухня", "M03", field="private_kitchenette", value="true"),
        atom("a3", "лифт", "M04", field="in_room_kettle", value="true"),  # ошибка модели
        or_groups=[{"group_id": "g1", "atom_ids": ["a2", "a3"]}],
    )
    ast = auto(text, reply)
    assert [c.feature_id for c in ast.hard] == ["A03"] and not ast.logic
    assert "M03" not in {c.feature_id for c in ast.soft}
    assert [s.text for s in ast.unparsed_spans] == ["кухня или чайник"]


# Как ответила модель в живом прогоне 27.10 (`tests/eval/parse_atoms_llm.csv`): промахи, которые
# в auto доходят до гостя.
V1_30 = (
    "Люблю спать с открытым окном: подтверждённая тишина ночью обязательна. "
    "Море из окна и бары недалеко были бы плюсом."
)
V1_30_REPLY = compact(
    atom("a1", "подтверждённая тишина ночью обязательна", "N05", field="LAeq_dBA", op="lte",
         value="30", unit="дБ", mapping="partial"),
    atom("a2", "Люблю спать с открытым окном", "N01", field="lnight_dba", op="lte", value="45",
         unit="дБ", mapping="partial"),
    atom("a3", "Море из окна", "V01", value="true", requiredness="desired"),
    atom("a4", "бары недалеко", "N04", field="count", op="gte", value="1",
         requiredness="desired"),
)  # fmt: skip
NEG_3 = "Обязательно без пересадок из аэропорта и вход без ступеней. Балкон не нужен."
NEG_3_BALCONY = atom(
    "a3", "Балкон не нужен", "M02", field="private_balcony", value="false", requiredness="desired"
)
NEG_3_REPLY = compact(
    atom("a1", "Обязательно без пересадок из аэропорта", "T01", field="transfers", op="eq",
         value="0"),
    atom("a2", "вход без ступеней", "A02", field="threshold_mm", op="lte", value="20", unit="мм"),
    NEG_3_BALCONY,
)  # fmt: skip


def test_auto_live_run_misses_do_not_reach_guest():
    """Ревью №28: промахи живого прогона, которые в auto видит гость, — проверки на регрессию
    (третий, show-6 «Удалёнка», — `test_auto_keeps_rules_and_takes_only_the_rest_from_model`).

    v1-30: из «Люблю спать с открытым окном» модель делала обязательную часть вне каталога, и
    ни один отель не мог стать «Подходит». Правила опирались на эти слова в условии тишины
    (окна открыты) — модель на них своё условие не ставит.
    neg-3: из «Балкон не нужен» модель делала M02. Снятое требование правила поняли."""
    ast = auto(V1_30, V1_30_REPLY)
    assert [(c.feature_id, c.field, c.mapping) for c in ast.hard] == [
        ("N05", "LAeq_dBA", "exact"),
        ("N05", "windows_state", "exact"),
    ]
    assert not [o for o in ast.out_of_catalog if o.requiredness == "required"]
    assert ("N04", "gte") in {(c.feature_id, c.op) for c in ast.soft}  # «бары» — от модели

    fake = FakeLLM([NEG_3_REPLY.model_dump()])
    ast = parse_query(NEG_3, today=TODAY, gateway=Gateway([fake])).ast
    assert fake.calls == []  # остатка нет — модель не зовём
    assert "M02" not in {c.feature_id for c in ast.hard + ast.soft}
    # Модель позвали из-за другого остатка — её M02 на снятом требовании всё равно не берём.
    text = "Еду на удалёнку. Обязательно вход без ступеней. Балкон не нужен."
    reply = compact(
        atom("a1", "на удалёнку", value="удалённая работа", mapping="unmatched",
             requiredness="unspecified"),
        NEG_3_BALCONY | {"requiredness": "required"},
    )  # fmt: skip
    ast = auto(text, reply)
    assert [c.feature_id for c in ast.hard] == ["A02"]
    assert "M02" not in {c.feature_id for c in ast.soft}


@pytest.mark.parametrize(
    "reply",
    [
        compact(atom("a1", "без ковролина", value="без ковролина", mapping="unmatched",
                     requiredness="unspecified")),
        compact(atom("a1", "без ковролина", value="без ковролина", mapping="unmatched",
                     requiredness="desired")),
        compact(context_text=["без ковролина"]),
        compact(atom("a1", "без ковролина", "M02", value="true", provenance="inferred")),
    ],
)  # fmt: skip
def test_auto_required_leftover_does_not_depend_on_model(reply):
    """Ревью №29: кусок у «обязательно», который правила не поняли, — обязательная часть вне
    каталога (D38). Модель ставила его желательным, клала в контекст или только в вопрос —
    защита снималась молча, и отель мог получить «Подходит»."""
    ast = auto("С собакой 10 кг, обязательно без ковролина.", reply)
    (part,) = [o for o in ast.out_of_catalog if o.text_span == "без ковролина"]
    assert part.requiredness == "required"
    # Точное условие модели на таком куске — обязательное, как сказал гость.
    reply = compact(
        atom("a1", "две отдельные кровати", "M01", field="bed_configuration", value="twin",
             requiredness="desired"),
    )  # fmt: skip
    ast = auto("Обязательно две отдельные кровати.", reply)
    assert [(c.feature_id, c.value) for c in ast.hard] == [("M01", "twin")] and not ast.soft


def test_auto_rules_bug_takes_model_whole(monkeypatch):
    """Ревью №29: при ошибке разбора правил модель звали, но объединяли с тем же сломанным
    разбором — платный вызов впустую и пометка «ответ модели не прошёл проверку». Теперь, как
    в режиме llm, — разбор модели целиком."""
    import importlib

    parse_module = importlib.import_module("agents.search.parse")  # имя занято функцией parse
    real = parse_module.parse_rules

    def broken(text, catalog, today):
        c, spans = real(text, catalog, today)
        atoms = [c.atoms[0], c.atoms[1].model_copy(update={"atom_id": c.atoms[0].atom_id})]
        return c.model_copy(update={"atoms": atoms + c.atoms[2:]}), spans

    monkeypatch.setattr(parse_module, "parse_rules", broken)
    fake = FakeLLM([REMOTE_REPLY])
    out = parse_query(REMOTE, today=TODAY, gateway=Gateway([fake]))
    assert len(fake.calls) == 1 and out.ast.parsed_by == "llm" and not out.problems
    assert parse_module.NOTE_LLM_INVALID not in out.notes
    assert [(c.feature_id, c.value) for c in out.ast.hard] == [("R03", 600), ("R01", 50)]
    assert "N01" in {c.feature_id for c in out.ast.soft}  # «потише вокруг» — от модели


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


def test_parse_eval_csv_matches_report(tmp_path):
    """CSV для общей метрики (tests/eval/metrics.py --parse): доля строк с expected == got —
    те же числа, что в нашем отчёте."""
    import csv

    from agents.eval.parse_eval import main

    out = tmp_path / "atoms.csv"
    args = ["--mode", "rules", "--ids", "v1-01", "v1-30", "show-6", "neg-4"]
    report = main([*args, "--out", str(tmp_path), "--csv", str(out)])
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {"query_id", "atom_id", "expected", "got", "requiredness"} <= set(rows[0])
    for kind in ("hard", "soft"):
        s = report["summary"][kind]
        mine = [r for r in rows if r["requiredness"] == kind]
        assert len(mine) == s["tp"] + s["fn"] + s["fp"]
        assert sum(r["expected"] == r["got"] for r in mine) == s["tp"]
    assert b"\r" not in out.read_bytes()  # ревью №28: CRLF менял при перевыгрузке весь файл


def test_committed_rules_csv_and_report_are_fresh():
    """Ревью №28: `parse_atoms_rules.csv` и отчёт правил ничем не сверялись и молча устаревали
    после новых эталонов. Режим rules детерминирован — свежий прогон совпадает с файлами (кроме
    замеров времени). Упал — перевыгрузить: `python -m agents.eval.parse_eval --mode rules
    --csv tests/eval/parse_atoms_rules.csv`."""
    import csv

    from agents.eval.parse_eval import REFERENCE, REPORTS, evaluate_case, markdown, summarize

    cases = [json.loads(line) for line in REFERENCE.read_text("utf-8").splitlines() if line]
    atoms: list[dict[str, str]] = []
    rows = [evaluate_case(c, "rules", atoms) for c in cases]
    path = REFERENCE.parent / "parse_atoms_rules.csv"
    assert b"\r" not in path.read_bytes()
    with path.open(encoding="utf-8", newline="") as f:
        assert list(csv.DictReader(f)) == atoms

    committed = json.loads((REPORTS / "parse_rules.json").read_text("utf-8"))
    summary = summarize(rows, "rules") | {"median_ms": committed["summary"]["median_ms"]}
    assert committed["summary"] == summary
    assert [c | {"ms": 0} for c in committed["cases"]] == [r | {"ms": 0} for r in rows]
    assert (REPORTS / "parse_rules.md").read_text("utf-8") == markdown(summary, rows)


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
        # «Никаких», «аллергия на» — тоже отрицание вплотную (ревью №29: «никаких собак» давало
        # аллергику обязательное «можно с собакой»).
        ("у меня аллергия: никаких собак", [("P01", "species", "in", ["cat", "none"])]),
        ("у меня аллергия на собак", [("P01", "species", "in", ["cat", "none"])]),
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


@pytest.mark.parametrize(
    "text",
    [
        "Лифт нам ни к чему.",
        "Балкон — без разницы.",
        "Лифт не требуется.",
        "Парковка не понадобится.",
        "Балкон не принципиален.",
        "Балкон нам всё равно.",
        "Лифт неважен.",
        "Лифт нам не нужен.",
        "Можно и без балкона.",
        "Можно без балкона.",
        "Без балкона тоже можно.",
        "Обойдёмся без лифта.",
    ],
)
def test_dismissed_in_other_words(text):
    """Ревью №29: отказ другими словами («ни к чему», «можно и без») правила делали
    обязательным условием — «лифт нужен» или «без балкона», и отели — «Не подходит». Теперь
    требование снято: условия нет, а кусок понят — модель его заново не разбирает."""
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert not ast.hard and not ast.soft and not ast.out_of_catalog
    assert not ast.unparsed_spans


@pytest.mark.parametrize(
    "text", ["Балкон не обязателен.", "Балкон — по желанию.", "Необязательно балкон."]
)
def test_not_required_in_other_words_is_desired(text):
    """Ревью №29: «не обязателен», «по желанию» — смягчение, а не обязательный балкон."""
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert not ast.hard and [c.feature_id for c in ast.soft] == ["M02"]
    assert not ast.unparsed_spans


def test_still_is_not_dismissal():
    """«Всё равно» в середине фразы — «всё-таки», а не отказ: лифт остаётся пожеланием."""
    text = "Хочется вид на горы, а лифт всё равно был бы удобен."
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert "A03" in {c.feature_id for c in ast.soft}


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


@pytest.mark.parametrize(
    ("text", "op", "meters"),
    [
        ("обязательно не дальше 500 м от моря", "lte", 500),
        ("обязательно не дальше 10 минут пешком до моря", "lte", 800),
        ("до пляжа обязательно не дольше 10 минут пешком", "lte", 800),
        ("обязательно не ближе 300 м от моря", "gte", 300),
    ],
)
def test_comparison_before_number_is_part_of_the_rule(text, op, meters):
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.op, c.value) for c in ast.hard] == [("W01", op, meters)]
    assert not ast.unparsed_spans and not ast.out_of_catalog  # «не дальше» не висит отдельно


def test_marker_before_colon_covers_the_list():
    ast = parse_query("Обязательно: тихо, без ковролина.", today=TODAY, mode="rules").ast
    (rest,) = [o for o in ast.out_of_catalog if is_leftover(o)]
    assert rest.text_span == "без ковролина" and rest.requiredness == "required"
    # Маркер после двоеточия «заголовок» не задевает: пояснение гостя остаётся просто непонятым.
    text = "Мама ходит с тростью: от улицы до номера обязательно без ступеней."
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [s.text for s in ast.unparsed_spans] == ["Мама ходит с тростью"]
    assert not [o for o in ast.out_of_catalog if is_leftover(o)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("обязательно не дальше 300 метров до остановки", ("W02", "lte", 300, "stop")),
        ("нужен номер не дальше 1 км от центра", ("W03", "lte", 1000, "center")),
        ("до метро не больше 5 минут пешком", ("W02", "lte", 400, "stop")),
    ],
)
def test_measured_distance_to_stop_and_center(text, expected):
    ast = parse_query(text, today=TODAY, mode="rules").ast
    (c,) = ast.hard
    assert (c.feature_id, c.op, c.value, c.params["target_kind"]) == expected
    assert not ast.unparsed_spans and not ast.out_of_catalog


def test_soft_negation_from_model_flips_or_asks():
    raw = "лучше не рядом с баром, не песчаный пляж, без лишнего"
    c = compact(
        atom(
            "a1",
            "не рядом с баром",
            "N04",
            field="count",
            op="gte",
            value="3",
            requiredness="desired",
        ),  # fmt: skip
        atom(
            "a2",
            "не песчаный пляж",
            "B01",
            field="surface",
            op="eq",
            value="sand",
            requiredness="desired",
        ),  # fmt: skip
        atom(
            "a3",
            "без лишнего",
            "W05",
            field="known_step_sections",
            op="exists",
            requiredness="desired",
        ),  # fmt: skip
        negated_atoms=["a1", "a2", "a3"],
    )
    ast = to_full_ast(c, raw, CAT, parsed_by="llm")
    assert [(s.feature_id, s.op, s.value) for s in ast.soft] == [
        ("N04", "lt", 3),
        ("B01", "ne", "sand"),
    ]
    (ask,) = ast.clarify  # «exists» не перевернуть — уточнение, а не прямое условие
    assert ask.about == "без лишнего"


@pytest.mark.parametrize(
    "text",
    [
        "Обязательно подальше от дороги.",
        "Подальше от трассы обязательно.",
        "Обязательно далеко от дороги.",
    ],
)
def test_far_from_road_is_required(text):
    """Ревью PR №8: «обязательно подальше от дороги» давало «Подходит» у всех отелей Сочи."""
    ast = parse_query(text, "sochi", today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.op, c.value) for c in ast.hard] == [("N02", "gte", 200)]
    assert not ast.unparsed_spans and not ast.out_of_catalog


def test_comparatives_are_part_of_condition_not_softeners():
    """«Подальше / поближе / повыше» — слова самого условия: одни — желательное, а «обязательно»
    их побеждает. Смягчают только «желательно», «хотелось бы», «лучше»…"""

    def split(text):
        ast = parse_query(text, "sochi", today=TODAY, mode="rules").ast
        return [c.feature_id for c in ast.hard], [c.feature_id for c in ast.soft]

    assert split("этаж повыше") == ([], ["floor"])
    assert split("обязательно этаж повыше") == (["floor"], [])
    assert split("обязательно поближе к морю") == (["W01"], [])
    assert split("хотелось бы подальше от шоссе") == ([], ["N02"])
    assert split("не у трассы") == ([], ["N02"])
    assert split("подальше от железной дороги") == ([], [])  # ж/д — N03, не дорога


def test_far_from_road_changes_statuses():
    from collections import Counter

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        body = c.post(
            "/api/search", json={"text": "обязательно подальше от дороги", "city_id": "sochi"}
        )
        statuses = Counter(r["status"] for r in body.json()["results"])
    assert statuses["not_fit"] and statuses["fits"]  # не «Подходит» у всех


def test_required_partial_negation_from_model_is_flipped_or_asked():
    """Ревью №11: обязательный частичный атом из negated_atoms уходил в мягкие неперевёрнутым."""
    raw = "обязательно не рядом с баром, обязательно без лишнего"
    c = compact(
        atom("a1", "не рядом с баром", "N04", field="count", op="gte", value="3",
             mapping="partial"),
        atom("a2", "без лишнего", "W05", field="known_step_sections", op="exists",
             mapping="partial"),
        negated_atoms=["a1", "a2"],
    )  # fmt: skip
    ast = to_full_ast(c, raw, CAT, parsed_by="llm")
    assert not ast.hard and [(s.feature_id, s.op, s.value) for s in ast.soft] == [("N04", "lt", 3)]
    # Обязательное частичное остаётся вне каталога (не «Подходит»); невыразимое — ещё и вопрос.
    assert sorted((o.atom_id, o.requiredness) for o in ast.out_of_catalog) == [
        ("a1", "required"),
        ("a2", "required"),
    ]
    (ask,) = ast.clarify
    assert ask.about == "без лишнего"


@pytest.mark.parametrize(
    ("text", "op", "value"),
    [
        ("не много баров рядом", "lte", 2),
        ("рядом не много баров", "lte", 2),
        ("не мало баров рядом", "gte", 3),
        ("не больше двух баров рядом", "lte", 2),
        ("минимум 3 бара рядом", "gte", 3),
        ("больше одного бара рядом", "gt", 1),
        # Ревью №21: «не очень много» давало ≥ 3, «немало» — ≤ 2.
        ("не очень много баров рядом", "lte", 2),
        ("немало баров рядом", "gte", 3),
    ],
)
def test_bars_negated_quantity_and_count(text, op, value):
    """Ревью №20: «не много баров рядом» давало N04 ≥ 3, а «не много» оставалось непонятым."""
    ast = parse_query(text, today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.op, c.value) for c in ast.soft] == [("N04", op, value)]
    assert not ast.unparsed_spans


def test_bars_range_gives_both_bounds():
    """Ревью №21: «от 2 до 5 баров» давало только ≤ 5 — «от 2» терялось без следа."""
    ast = parse_query("обязательно от 2 до 5 баров рядом", today=TODAY, mode="rules").ast
    assert [(c.feature_id, c.op, c.value) for c in ast.hard] == [
        ("N04", "gte", 2),
        ("N04", "lte", 5),
    ]
    assert not ast.soft and not ast.unparsed_spans and not ast.out_of_catalog


def test_bars_rule_does_not_invert_required():
    """Ревью №21: «до» внутри «надо», потерянное число и отрицание не вплотную давали
    обязательное N04 наоборот. Нет ни числа, ни слова о количестве или рядом отрицание —
    условия нет: фраза непонятая или обязательная часть вне каталога (D38)."""

    def parsed(text):
        ast = parse_query(text, "sochi", today=TODAY, mode="rules").ast
        return [(c.feature_id, c.op, c.value) for c in ast.hard + ast.soft], ast

    assert parsed("обязательно надо два бара рядом")[0] == [("N04", "gte", 2)]
    assert parsed("обязательно 2 бара рядом")[0] == [("N04", "gte", 2)]
    conds, ast = parsed("обязательно чтобы не было бара рядом")
    assert conds == [] and [o.requiredness for o in ast.out_of_catalog] == ["required"]
    assert parsed("не хочу бара рядом")[0] == [] and parsed("бары рядом")[0] == []
    assert parsed("бары рядом не нравятся")[0] == []
    # Отрицание при числе не вплотную: на ревью №21 — обязательное N04 ≥ 2 и «Подходит» у отеля
    # с тремя барами. Теперь фраза непонятая, у «обязательно» — обязательная вне каталога.
    for text in (
        "обязательно чтобы 2 бара рядом не было",
        "обязательно 2 бара рядом не надо",
        "обязательно больше двух баров рядом не надо",
        "обязательно, чтобы не было 2 баров рядом",
        "обязательно, чтобы не было больше двух баров рядом",
    ):
        conds, ast = parsed(text)
        assert conds == [], text
        assert [o.requiredness for o in ast.out_of_catalog] == ["required"], text
    for text in (
        "не хочу больше двух баров рядом",
        "мне не нужно 2 бара рядом",
        "больше двух баров рядом не надо",
        "ни одного бара рядом",
    ):
        conds, ast = parsed(text)
        assert conds == [] and ast.unparsed_spans, text
    # «Не больше двух» — отрицание внутри самого сравнения: условие ставится, и в нужную сторону.
    assert parsed("обязательно чтобы было не больше двух баров рядом")[0] == [("N04", "lte", 2)]
    # Исключения «симпатии» больше нет (D41): «бары нравятся», «были бы плюсом» и отрицание
    # не вплотную («не очень люблю») разбирает модель, правило условие не ставит.
    for text in (
        "бары рядом нравятся",
        "Море из окна и бары недалеко были бы плюсом.",
        "Бары рядом не очень люблю — это обязательно",
        "бары рядом мне не особо нравятся",
        "бары рядом совсем не в плюс",
    ):
        conds, _ = parsed(text)
        assert "N04" not in {f for f, _, _ in conds}, text
    # «Тихо» — свои N02 и N04 ≤ 2, без мягкого N04 ≥ 3 от «нравятся».
    conds, _ = parsed("Бары рядом нам не слишком нравятся, обязательно тихо")
    assert conds == [("N02", "gte", 200), ("N04", "lte", 2)]
