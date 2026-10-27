"""Разбор запроса гостя: текст → `QueryAST` (контракт 3).

Порядок (v4b §9): правила → если правила покрыли весь текст, модель не нужна → иначе LLM →
перевод в полный AST и проверка → при любой неудаче LLM — результат правил с пометкой
«офлайн». Неразобранное остаётся в `unparsed_spans`; ничего не теряется молча.

В режиме `auto` модель — только для остатка: она видит весь запрос, но из её ответа берутся
атомы про куски, которые правила не поняли. Понятое правилами остаётся как есть — модель его
не переписывает (на «поменьше баров рядом» в сценарии «Удалёнка» она теряла условие), кроме
прямого отрицания и ИЛИ / НЕ поверх атомов правил (D41, `merge_rest`).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from agents.catalog import Catalog, Feature, coerce_value, load_catalog
from agents.llm import Gateway, LLMError, LLMResult, get_gateway
from agents.query_ast import QueryAST
from agents.search.compact import (
    CompactAST,
    CompactAtom,
    compact_schema,
    is_leftover,
    to_full_ast,
)
from agents.search.negation import negate
from agents.search.prompt import system_prompt, user_prompt
from agents.search.rules import parse_rules
from agents.search.text import find_span, normalize
from agents.search.validate import problems

log = logging.getLogger("agents.search")

NOTE_OFFLINE = "Запрос разобран правилами (офлайн-режим): ключ LLM не задан."
NOTE_LLM_FAILED = "Запрос разобран правилами (офлайн-режим): модель недоступна."
NOTE_LLM_INVALID = "Запрос разобран правилами: ответ модели не прошёл проверку."
NOTE_UNPARSED = "Часть запроса не разобрана — она показана отдельно и не считается выполненной."

CACHE_SIZE = 256
# Кэш ответов модели: AST и пометки. Наружу — копии: вызывающий код может менять свой AST.
_cache: OrderedDict[tuple, tuple[QueryAST, list[str]]] = OrderedDict()


@dataclass
class ParseOutcome:
    ast: QueryAST
    notes: list[str] = field(default_factory=list)
    llm: LLMResult | None = None
    problems: list[str] = field(default_factory=list)


def parse(text: str, city_id: str | None = None, **kw) -> QueryAST:
    return parse_query(text, city_id, **kw).ast


def parse_query(
    text: str,
    city_id: str | None = None,
    *,
    gateway: Gateway | None = None,
    today: date | None = None,
    mode: Literal["auto", "rules", "llm"] = "auto",
) -> ParseOutcome:
    """`auto` — правила, модель только для остатка; `rules` — без модели; `llm` — всегда модель."""
    catalog = load_catalog()
    today = today or date.today()
    compact, spans = parse_rules(text, catalog, today)
    rules_ast = to_full_ast(compact, text, catalog, parsed_by="rules", city_id=city_id, spans=spans)
    rules_problems = problems(rules_ast, catalog)
    if rules_problems:  # ошибка в правилах — это баг, но запрос всё равно обслуживаем
        log.error("rules ast problems: %s", rules_problems)

    # Непонятое правилами: отдельные куски и обязательные части без характеристики.
    leftovers = bool(rules_ast.unparsed_spans) or any(
        is_leftover(o) for o in rules_ast.out_of_catalog
    )

    def offline(note: str | None, errs: list[str] | None = None) -> ParseOutcome:
        notes = [note] if note else []
        if leftovers:
            notes.append(NOTE_UNPARSED)
        return ParseOutcome(rules_ast, notes, problems=errs or rules_problems)

    gw = gateway or get_gateway()
    if mode == "rules":
        return offline(None)
    if not gw.available:  # пометка «офлайн» — даже если правила справились сами
        return offline(NOTE_OFFLINE)
    if mode == "auto" and not leftovers and not rules_problems:
        return offline(None)
    key = (normalize(text).strip(), city_id, catalog.version, today.isoformat(), mode)
    if key in _cache:
        _cache.move_to_end(key)
        cached, notes = _cache[key]
        return ParseOutcome(cached.model_copy(deep=True), list(notes))
    try:
        done = gw.complete(
            task="parse_query",
            system=system_prompt(catalog),
            user=user_prompt(text, city_id, today),
            schema=compact_schema(),
            model=CompactAST,
            max_tokens=8192,
        )
    except LLMError:
        return offline(NOTE_LLM_FAILED)
    if mode == "auto" and not rules_problems:
        # Куски у «обязательно», которые правила не поняли (D38), — обязательны и у модели.
        rest = [(o.span.start, o.span.end) for o in rules_ast.out_of_catalog if is_leftover(o)]
        merged, merged_spans = merge_rest(compact, spans, done.value, text, catalog, rest)
        ast = to_full_ast(
            merged, text, catalog, parsed_by="llm", city_id=city_id, spans=merged_spans
        )
    else:
        # Режим llm — или ошибка в разборе правил: тогда объединять не с чем, модель целиком.
        ast = to_full_ast(done.value, text, catalog, parsed_by="llm", city_id=city_id)
    errs = problems(ast, catalog)
    if errs:
        log.warning("llm ast problems: %s", errs)
        return offline(NOTE_LLM_INVALID, errs)
    rest = ast.unparsed_spans or any(is_leftover(o) for o in ast.out_of_catalog)
    notes = [NOTE_UNPARSED] if rest else []
    _cache[key] = (ast.model_copy(deep=True), list(notes))
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return ParseOutcome(ast, notes, llm=done.result)


def merge_rest(
    rules: CompactAST,
    spans: dict[str, tuple[int, int]],
    model: CompactAST,
    text: str,
    catalog: Catalog,
    rest: list[tuple[int, int]] | None = None,
) -> tuple[CompactAST, dict[str, tuple[int, int]]]:
    """Понятое правилами + атомы модели про непонятый остаток (D41).

    - Атом модели на куске, который правила не поняли, берём как есть.
    - Атом модели на куске атома правил с той же характеристикой и полем — это тот же атом:
      условие остаётся от правил, а ИЛИ модели переводится на атом правил («аптека или
      супермаркет»). Модель прямо отрицает атом правил на том же куске («лифт нам ни к чему»
      против «нужен лифт», НЕ над «собак» в «никаких собак») — атом правил снимается, берётся
      атом модели со своим НЕ. Другая характеристика на куске правил — не берём: правила
      точнее на своих шаблонах.
    - Группа ИЛИ модели — только целиком: не перенесли один вариант — не берём и остальные (их
      куски остаются непонятыми). Половина «ИЛИ» не становится обязательным «И».
    - Атом, фрагмента которого нет в запросе, не берём — это додуманное.
    - Контекст (город, даты, поездка) берём у обоих: он не создаёт условий, только покрывает
      текст.
    - `rest` — куски у «обязательно», которые правила не поняли (D38). Атом модели на них —
      обязательный, какую бы обязательность ни поставила модель; выведенный атом (только
      вопрос) и контекст модели их не покрывают. Так «не выше „Нужно уточнить“» не зависит от
      модели.
    """
    taken = [span for key, span in spans.items() if not key.startswith("ctx")]
    by_id = {a.atom_id: a for a in rules.atoms}
    rest = rest or []

    def touches(span: tuple[int, int], other: tuple[int, int]) -> bool:
        return span[0] < other[1] and other[0] < span[1]

    def free(span: tuple[int, int]) -> bool:
        return not any(touches(span, s) for s in taken)

    def on_rest(span: tuple[int, int]) -> bool:
        return any(touches(span, s) for s in rest)

    # Что делаем с каждым атомом модели: free — взять, same — это атом правил (id), negation —
    # снять атом правил (id) и взять атом модели, drop — не брать.
    plan: dict[str, tuple[str, str | None]] = {}
    where: dict[str, tuple[int, int]] = {}
    for atom in model.atoms:
        found = find_span(text, atom.text)
        if found is None:
            continue
        span = where[atom.atom_id] = (found.start, found.end)
        if free(span):
            # Выведенный атом стал бы только вопросом и снял бы с куска D38 молча.
            inferred = atom.provenance == "inferred" and on_rest(span)
            plan[atom.atom_id] = ("drop", None) if inferred else ("free", None)
            continue
        negated = atom.atom_id in model.negated_atoms
        rel = {
            key: _relation(by_id[key], atom, negated, catalog)
            for key, s in spans.items()
            if key in by_id and touches(span, s)
        }
        same = [k for k, r in rel.items() if r == "same"]
        flipped = [k for k, r in rel.items() if r == "negation"]
        plan[atom.atom_id] = (
            ("same", same[0]) if same else ("negation", flipped[0]) if flipped else ("drop", None)
        )

    def kind(atom_id: str) -> str:
        return plan.get(atom_id, ("drop", None))[0]

    # Модель сама себе противоречит (тот же атом правил и подтверждён, и отрицается) — атом
    # правил остаётся, отрицание не берём.
    confirmed = {t for k, t in plan.values() if k == "same"}
    for a, (k, t) in list(plan.items()):
        if k == "negation" and t in confirmed:
            plan[a] = ("drop", None)
    # ИЛИ модели — целиком или никак: вариант, который не перенести, гасит и остальные.
    groups_in_model = [g for g in model.or_groups if len(g.atom_ids) >= 2]
    changed = True
    while changed:
        changed = False
        for g in groups_in_model:
            if all(kind(a) != "drop" for a in g.atom_ids):
                continue
            for a in g.atom_ids:
                if kind(a) in ("free", "negation"):
                    plan[a] = ("drop", None)
                    changed = True

    removed = {t for k, t in plan.values() if k == "negation"}
    spans = {k: s for k, s in spans.items() if k not in removed}
    atoms = [a for a in rules.atoms if a.atom_id not in removed]
    ids: dict[str, str] = {}  # id атома модели → id в общем разборе
    replaced: dict[str, str] = {}  # снятый атом правил → атом модели вместо него
    for atom in model.atoms:
        k, target = plan.get(atom.atom_id, ("drop", None))
        if k == "same" and target:
            ids[atom.atom_id] = target
        elif k in ("free", "negation"):
            new_id = ids[atom.atom_id] = f"m{sum(a.startswith('m') for a in ids.values()) + 1}"
            if target:
                replaced[target] = new_id
            spans[new_id] = where[atom.atom_id]
            atoms.append(atom.model_copy(update={"atom_id": new_id}))
    groups = [
        g.model_copy(update={"atom_ids": [replaced.get(a, a) for a in g.atom_ids]})
        for g in rules.or_groups
    ]
    for g in groups_in_model:
        members = list(dict.fromkeys(ids[a] for a in g.atom_ids if a in ids))
        if len(members) < 2 or not all(a in ids for a in g.atom_ids):
            continue
        groups.append(g.model_copy(update={"group_id": f"m{g.group_id}", "atom_ids": members}))
        atoms = _same_requiredness(atoms, members, set(by_id))
    # Кусок у «обязательно» (D38) модель могла счесть желательным — он всё равно обязательный.
    required = {
        new for a, new in ids.items() if kind(a) in ("free", "negation") and on_rest(where[a])
    }
    atoms = [
        a.model_copy(update={"requiredness": "required", "importance": "medium"})
        if a.atom_id in required
        else a
        for a in atoms
    ]
    # НЕ модели — у взятых атомов модели. У «того же атома» отрицание уже учтено сравнением:
    # перенести его на атом правил — значит перевернуть «без собак» второй раз.
    negated_ids = list(rules.negated_atoms) + [
        ids[a] for a in model.negated_atoms if a in ids and kind(a) in ("free", "negation")
    ]

    def context_ok(c: str) -> bool:
        found = find_span(text, c)
        return c not in rules.context_text and not (found and on_rest((found.start, found.end)))

    context = list(rules.context_text) + [c for c in model.context_text if context_ok(c)]
    clarify = list(rules.clarify)
    for c in model.clarify:
        about = find_span(text, c.about)
        if (about is None or free((about.start, about.end))) and all(
            c.about != x.about for x in clarify
        ):
            clarify.append(c)
    merged = rules.model_copy(
        update={
            "city": rules.city or model.city,
            "date_from": rules.date_from or model.date_from,
            "date_to": rules.date_to or model.date_to,
            "adults": rules.adults or model.adults,
            "children_ages": rules.children_ages or model.children_ages,
            "pets": rules.pets or model.pets,
            "context_text": context,
            "atoms": atoms,
            "or_groups": groups,
            "negated_atoms": negated_ids,
            "clarify": clarify,
        }
    )
    return merged, spans


_LESS = ("lte", "lt")
_MORE = ("gte", "gt")


def _relation(rule: CompactAtom, atom: CompactAtom, negated: bool, catalog: Catalog) -> str:
    """Атом модели против атома правил на том же куске: `same` — тот же атом (условие — от
    правил), `negation` — модель прямо отрицает условие правил, `other` — другая
    характеристика или поле. Сравнить смысл не вышло — `same`: понятое правилами остаётся."""
    feature = catalog.get(rule.feature_id)
    if feature is None or atom.feature_id != rule.feature_id:
        return "other"
    field = rule.field or feature.default_field
    if (atom.field or feature.default_field) != field:
        return "other"
    ours = _sense(feature, field, rule.op, rule.value, rule.unit, catalog)
    theirs = _sense(feature, field, atom.op, atom.value, atom.unit, catalog)
    if negated:
        if ours is not None and theirs == ours:
            return "negation"  # НЕ модели над тем же условием: «никаких собак»
        flipped = negate(feature, field, atom.op, atom.value)
        theirs = _sense(feature, field, *flipped, atom.unit, catalog) if flipped else None
    if ours is None or theirs is None or ours[0] != theirs[0]:
        return "same"
    if ours[0] == "set":
        return "same" if ours[1] & theirs[1] else "negation"
    if ours[0] == "cmp":  # «≠ нет» против «= нет»
        return "negation" if ours[2] == theirs[2] and ours[1] != theirs[1] else "same"
    return "negation" if ours[1] != theirs[1] else "same"


def _sense(
    feature: Feature, field: str | None, op: str, value: str, unit: str, catalog: Catalog
) -> tuple | None:
    """Смысл условия для сравнения: сторона числа, множество значений перечисления или
    да / нет. Не сравнить — None."""
    op = op or feature.ops[0]
    spec = feature.field_spec(field)
    if op in _LESS or op in _MORE:
        return ("dir", op in _MORE)
    if spec is not None and spec.type == "enum" and op in ("eq", "ne", "in"):
        values = {v.strip() for v in value.split(",") if v.strip()}
        return ("set", frozenset(values if op != "ne" else set(spec.values) - values))
    if op not in ("eq", "ne"):
        return None
    if spec is None or spec.type == "bool":
        try:
            yes = coerce_value(spec, value or "true", unit, catalog.walk_speed_m_per_min)
        except ValueError:
            return None
        return ("bool", yes == (op == "eq")) if isinstance(yes, bool) else None
    return ("cmp", op, value.strip().lower())


def _same_requiredness(
    atoms: list[CompactAtom], members: list[str], rule_ids: set[str]
) -> list[CompactAtom]:
    """ИЛИ с атомом правил: варианты модели — с той же обязательностью, иначе обязательной
    осталась бы только половина группы."""
    of_rules = {a.requiredness for a in atoms if a.atom_id in members and a.atom_id in rule_ids}
    if len(of_rules) != 1:
        return atoms
    (req,) = of_rules
    return [
        a.model_copy(update={"requiredness": req})
        if a.atom_id in members and a.atom_id not in rule_ids
        else a
        for a in atoms
    ]
