"""Поиск по готовому AST → три статуса для гостя (D31). Работает с любым `core.store.Store`.

Правила:
- оценка не закрывает обязательное условие (D18), неизвестное не становится «нет» (D5);
- у условия с `quantifier: any` («хотя бы где-то») хватает одного «да»;
- факты одной характеристики группируются по «месту»: субъект (отель, корпус, маршрут) и
  параметры. Условие проверяется на каждом месте; «да» — только если «да» везде, «нет» — если
  «нет» везде. Иначе «нужно уточнить»: у отеля несколько корпусов, и неизвестно, где окажется
  гость (D17, размещения);
- если на одном месте пригодные факты разных рецептов или источников расходятся, это
  противоречие (core §6): «нужно уточнить», ID обоих фактов в ответе.

Пока проверка — на уровне отеля и его корпусов и маршрутов. Размещения категорий
(этаж × окна, D17) добавятся вместе с фактами уровня номера.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agents.query_ast import Condition, QueryAST
from core.facts import Fact, Status
from core.store import Store
from core.verdict import GUEST_LABELS, GuestStatus, Verdict, guest_status

EXAMPLE_AST = (
    Path(__file__).resolve().parents[1] / "agents" / "schemas" / "examples" / "track_query.json"
)
DEFAULT_FIELDS = (
    "distance_m",
    "count",
    "mean",
    "mean_C",
    "aboveground_levels",
    "known_step_sections",
    "known_lit_percent",
)
ORDER = {GuestStatus.fits: 0, GuestStatus.needs_check: 1, GuestStatus.not_fit: 2}


def example_ast() -> QueryAST:
    return QueryAST.model_validate(json.loads(EXAMPLE_AST.read_text("utf-8")))


def _number(fact: Fact, field: str | None) -> Any:
    v = fact.value
    if isinstance(v, dict):
        if field:
            return v.get(field)
        return next((v[k] for k in DEFAULT_FIELDS if k in v), None)
    return v


def _compare(op: str, left: Any, right: Any) -> bool | None:
    if left is None:
        return None
    try:
        return {
            "eq": lambda: left == right,
            "ne": lambda: left != right,
            "lt": lambda: left < right,
            "lte": lambda: left <= right,
            "gt": lambda: left > right,
            "gte": lambda: left >= right,
            "in": lambda: left in right,
            "exists": lambda: True,
        }[op]()
    except TypeError:
        return None


def _params_match(fact: Fact, params: dict[str, Any]) -> bool:
    return all(fact.params.get(k) == v for k, v in params.items())


def _place(fact: Fact) -> tuple[str, str]:
    """Место факта: субъект и параметры. Разные рецепты одного места — кандидаты на противоречие."""
    return fact.subject_id, json.dumps(fact.params, sort_keys=True, ensure_ascii=False, default=str)


def evaluate(facts: list[Fact], c: Condition, hard: bool) -> dict[str, Any]:
    """Одно условие для одного отеля по его фактам этой характеристики."""
    facts = [f for f in facts if _params_match(f, c.params)]
    out: dict[str, Any] = {"feature_id": c.feature_id, "text_span": c.text_span, "fact_ids": []}
    if not facts:
        return out | {"verdict": Verdict.unknown, "reason": "нет факта"}
    if all(f.status == Status.not_applicable for f in facts):
        return out | {
            "verdict": Verdict.no,
            "reason": "неприменимо",
            "fact_ids": [f.fact_id for f in facts],
        }
    known = [f for f in facts if f.status not in (Status.unknown, Status.not_applicable)]
    usable = [f for f in known if f.usable_in_hard] if hard else known
    if not usable:
        reason = (
            "есть только оценка — в обязательных условиях не участвует (D18)"
            if known
            else (facts[0].unknown_reason or "неизвестно")
        )
        return out | {
            "verdict": Verdict.unknown,
            "reason": reason,
            "fact_ids": [f.fact_id for f in facts],
        }

    out["fact_ids"] = [f.fact_id for f in usable]
    out["statuses"] = sorted({f.status for f in usable})
    places: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for f in usable:
        places[_place(f)].append(f)

    verdicts: list[Verdict] = []
    conflict_ids: list[str] = []
    for fs in places.values():
        results = {_compare(c.op, _number(f, c.field), c.value) for f in fs}
        if results == {True}:
            verdicts.append(Verdict.yes)
        elif results == {False}:
            verdicts.append(Verdict.no)
        else:
            verdicts.append(Verdict.unknown)
            if {True, False} <= results:
                conflict_ids += [f.fact_id for f in fs]

    if all(v == Verdict.yes for v in verdicts) or (
        c.quantifier == "any" and Verdict.yes in verdicts
    ):
        return out | {"verdict": Verdict.yes}
    if all(v == Verdict.no for v in verdicts):
        return out | {"verdict": Verdict.no}
    if conflict_ids:
        return out | {
            "verdict": Verdict.unknown,
            "reason": "противоречие: источники расходятся — нужен ответ отельера",
            "conflict_fact_ids": conflict_ids,
        }
    if Verdict.yes in verdicts and Verdict.no in verdicts:
        return out | {
            "verdict": Verdict.unknown,
            "reason": "зависит от корпуса или маршрута — нужно уточнить у отеля",
        }
    return out | {"verdict": Verdict.unknown, "reason": "значение не сравнимо с условием"}


def search(store: Store, ast: QueryAST) -> list[dict[str, Any]]:
    hotels = store.hotels(ast.city_id or None)
    ids = [h["hotel_id"] for h in hotels]
    # Один запрос к хранилищу на характеристику, а не на отель.
    features = {c.feature_id for c in [*ast.hard, *ast.soft]}
    facts = {fid: store.facts_for(fid, ids) for fid in features}

    results = []
    for hotel in hotels:
        hid = hotel["hotel_id"]
        hard = [evaluate(facts[c.feature_id].get(hid, []), c, hard=True) for c in ast.hard]
        # Обязательная часть запроса вне каталога (D34) не засчитывается как выполненная.
        hard += [
            {
                "atom_id": o.atom_id,
                "text_span": o.text_span,
                "verdict": Verdict.unknown,
                "reason": "вне каталога: такое мы пока не знаем",
                "fact_ids": [],
            }
            for o in ast.out_of_catalog
            if o.requiredness == "required"
        ]
        soft = [
            evaluate(facts[c.feature_id].get(hid, []), c, hard=False) | {"weight": c.weight}
            for c in ast.soft
        ]
        status = guest_status(h["verdict"] for h in hard)
        score_min = sum(s["weight"] for s in soft if s["verdict"] == Verdict.yes)
        score_max = score_min + sum(s["weight"] for s in soft if s["verdict"] == Verdict.unknown)
        results.append(
            {
                "hotel": hotel,
                "status": status,
                "status_label": GUEST_LABELS[status],
                "hard": hard,
                "soft": soft,
                "score": {"min": round(score_min, 3), "max": round(score_max, 3)},
            }
        )
    results.sort(key=lambda r: (ORDER[r["status"]], -r["score"]["max"], -r["score"]["min"]))
    return results
