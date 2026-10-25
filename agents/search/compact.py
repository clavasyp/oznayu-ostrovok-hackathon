"""Компактный AST — рабочая схема для модели и офлайн-правил, и перевод в полный `QueryAST`.

Схема под лимиты structured outputs Claude (v4b): все поля обязательные, пустое — `""` или `[]`,
значения строками, без union, `oneOf` и рекурсии. Позиции фрагментов, типы значений, веса,
привязку и логику проставляет сервер (`to_full_ast`), а не модель.

Ни один атом не теряется: без характеристики или с другим смыслом — `out_of_catalog` (D34),
выведенный, а не сказанный гостем, — только уточнение; частичное соответствие — только мягкое
условие, обязательное частичное ещё и `out_of_catalog`, чтобы не дать «Подходит» (D5).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.catalog import Catalog, coerce_value
from agents.query_ast import (
    Clarification,
    Condition,
    Dates,
    Guests,
    LogicNode,
    OutOfCatalog,
    QueryAST,
    SoftCondition,
    Span,
)
from agents.search.text import covered_gaps, find_span

CITY_IDS = ("sochi", "moscow", "spb", "kazan", "kaliningrad", "minsk", "istanbul", "dubai")
IMPORTANCE = {"low": 1, "medium": 2, "high": 3}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Param(_Strict):
    key: str
    value: str


class CompactAtom(_Strict):
    atom_id: str
    text: str
    requiredness: Literal["required", "desired", "unspecified"]
    provenance: Literal["explicit", "inferred"]
    feature_id: str
    field: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "exists", ""]
    value: str
    unit: str
    params: list[Param]
    mapping: Literal["exact", "partial", "different_meaning", "unmatched"]
    importance: Literal["low", "medium", "high"]


class OrGroup(_Strict):
    group_id: str
    atom_ids: list[str]


class CompactClarify(_Strict):
    question: str
    about: str
    options: list[str]


class CompactAST(_Strict):
    city: str
    date_from: str
    date_to: str
    adults: str
    children_ages: list[str]
    pets: list[str]
    context_text: list[str]
    atoms: list[CompactAtom]
    or_groups: list[OrGroup]
    negated_atoms: list[str]
    clarify: list[CompactClarify]


def compact_schema() -> dict[str, Any]:
    return CompactAST.model_json_schema()


def to_full_ast(
    compact: CompactAST,
    raw_text: str,
    catalog: Catalog,
    *,
    parsed_by: Literal["llm", "rules"],
    city_id: str | None = None,
    spans: dict[str, tuple[int, int]] | None = None,
) -> QueryAST:
    """Перевод компактного AST в полный. `spans` — точные позиции от правил (иначе — поиск)."""
    spans = spans or {}
    hard: list[Condition] = []
    soft: list[tuple[CompactAtom, dict[str, Any]]] = []
    ooc: list[OutOfCatalog] = []
    clarify = [Clarification(**c.model_dump()) for c in compact.clarify]
    covered: list[tuple[int, int]] = []

    for i, text in enumerate(compact.context_text):
        if f"ctx{i}" in spans:
            covered.append(spans[f"ctx{i}"])
        elif s := find_span(raw_text, text):
            covered.append((s.start, s.end))

    for atom in compact.atoms:
        span = _span(raw_text, atom, spans)
        if span is None:
            # Фрагмента нет в запросе — модель додумала. Додуманное не становится условием.
            clarify.append(_ask(atom, catalog))
            continue
        covered.append((span.start, span.end))
        feature = catalog.get(atom.feature_id) if atom.feature_id else None
        base = {"atom_id": atom.atom_id, "span": span, "text_span": span.text}
        if feature is None or atom.mapping in ("unmatched", "different_meaning"):
            ooc.append(_out(atom, span, "missing_feature", [atom.feature_id] if feature else []))
            continue
        if atom.provenance == "inferred":
            clarify.append(_ask(atom, catalog))
            continue
        field = atom.field or feature.default_field
        if feature.fields and field is not None and field not in feature.fields:
            ooc.append(_out(atom, span, "unsupported_param", [feature.id]))
            continue
        try:
            value = _value(atom, feature, field, catalog)
        except ValueError:
            ooc.append(_out(atom, span, "unsupported_param", [feature.id]))
            continue
        cond = base | {
            "feature_id": feature.id,
            "op": atom.op or feature.ops[0],
            "value": value,
            "field": field,
            "params": {p.key: _param_value(p.value) for p in atom.params},
            "binding": "same_placement" if feature.room_level else "hotel",
            "quantifier": "all",
            "provenance": atom.provenance,
            "mapping": atom.mapping,
        }
        if atom.atom_id in compact.negated_atoms and atom.requiredness != "required":
            if isinstance(value, bool):
                cond["value"] = not value
        if atom.requiredness == "required" and atom.mapping == "exact":
            hard.append(Condition(**cond))
        else:
            soft.append((atom, cond))
            if atom.requiredness == "required":
                # Частичное соответствие не закрывает обязательное (D5):
                # эта часть остаётся вне каталога.
                ooc.append(_out(atom, span, "missing_feature", [feature.id], "partial"))

    total = sum(IMPORTANCE[a.importance] for a, _ in soft)
    soft_conds = [
        SoftCondition(**c, weight=round(IMPORTANCE[a.importance] / total, 4)) for a, c in soft
    ]
    logic, root = _logic(compact, hard)
    return QueryAST(
        raw_text=raw_text,
        city_id=city_id or (compact.city if compact.city in CITY_IDS else None),
        dates=_dates(compact),
        guests=_guests(compact),
        hard=hard,
        soft=soft_conds,
        clarify=clarify,
        out_of_catalog=ooc,
        logic=logic,
        hard_root=root,
        unparsed_spans=covered_gaps(raw_text, covered),
        parsed_by=parsed_by,
    )


def _span(raw: str, atom: CompactAtom, spans: dict[str, tuple[int, int]]) -> Span | None:
    if atom.atom_id in spans:
        start, end = spans[atom.atom_id]
        return Span(start=start, end=end, text=raw[start:end])
    return find_span(raw, atom.text)


def _value(atom: CompactAtom, feature, field: str | None, catalog: Catalog) -> Any:
    spec = feature.field_spec(field)
    op = atom.op or feature.ops[0]
    if op == "exists":
        return None
    if op == "in":
        return [
            coerce_value(spec, v, atom.unit, catalog.walk_speed_m_per_min)
            for v in atom.value.split(",")
            if v.strip()
        ]
    if not atom.value.strip() and (spec is None or spec.type == "bool"):
        return True  # «с балконом», «вид на море» — утверждение «есть»
    return coerce_value(spec, atom.value, atom.unit, catalog.walk_speed_m_per_min)


def _param_value(v: str) -> Any:
    v = v.strip()
    if v.isdigit():
        return int(v)
    if v.startswith(("[", "{")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _out(
    atom: CompactAtom,
    span: Span,
    kind: str,
    nearest: list[str],
    mapping_status: str | None = None,
) -> OutOfCatalog:
    nearest = nearest + [p.value for p in atom.params if p.key == "nearest_contract"]
    return OutOfCatalog(
        text_span=span.text,
        normalized=atom.value.strip() or span.text,
        kind="subjective" if atom.feature_id == "subjective" else kind,
        atom_id=atom.atom_id,
        span=span,
        requiredness=atom.requiredness,
        mapping_status=mapping_status
        or ("different_meaning" if atom.mapping == "different_meaning" else "unmatched"),
        nearest_contracts=[n for n in dict.fromkeys(nearest) if n and n != "subjective"],
    )


def _ask(atom: CompactAtom, catalog: Catalog) -> Clarification:
    feature = catalog.get(atom.feature_id)
    what = feature.short if feature else atom.text
    return Clarification(question=f"Учитывать «{what}»?", about=atom.text, options=["да", "нет"])


def _logic(compact: CompactAST, hard: list[Condition]) -> tuple[list[LogicNode], str | None]:
    """OR-группы и отрицания обязательных условий → граф; без них — пусто (все hard через AND)."""
    hard_ids = {c.atom_id for c in hard}
    groups = [g for g in compact.or_groups if len(set(g.atom_ids) & hard_ids) >= 2]
    negated = [a for a in compact.negated_atoms if a in hard_ids]
    if not groups and not negated:
        return [], None
    nodes: dict[str, LogicNode] = {}
    leaf: dict[str, str] = {}
    for c in hard:
        node_id = f"n_{c.atom_id}"
        nodes[node_id] = LogicNode(node_id=node_id, op="ATOM", atom_id=c.atom_id)
        if c.atom_id in negated:
            nodes[f"not_{c.atom_id}"] = LogicNode(
                node_id=f"not_{c.atom_id}", op="NOT", children=[node_id]
            )
            node_id = f"not_{c.atom_id}"
        leaf[c.atom_id] = node_id
    top: list[str] = []
    grouped: set[str] = set()
    for g in groups:
        members = [leaf[a] for a in g.atom_ids if a in leaf and a not in grouped]
        if len(members) < 2:
            continue
        grouped.update(a for a in g.atom_ids if a in leaf)
        node_id = f"or_{g.group_id}"
        nodes[node_id] = LogicNode(node_id=node_id, op="OR", children=members)
        top.append(node_id)
    top += [leaf[c.atom_id] for c in hard if c.atom_id not in grouped]
    nodes["root"] = LogicNode(node_id="root", op="AND", children=top)
    return list(nodes.values()), "root"


def _dates(c: CompactAST) -> Dates | None:
    try:
        d_from = date.fromisoformat(c.date_from) if c.date_from else None
        d_to = date.fromisoformat(c.date_to) if c.date_to else None
    except ValueError:
        return None
    if d_from is None and d_to is None:
        return None
    return Dates.model_validate({"from": d_from, "to": d_to})


def _guests(c: CompactAST) -> Guests | None:
    adults = int(c.adults) if c.adults.strip().isdigit() else None
    ages = [int(a) for a in c.children_ages if a.strip().isdigit()]
    if adults is None and not ages and not c.pets:
        return None
    return Guests(adults=adults, children_ages=ages, pets=list(c.pets))
