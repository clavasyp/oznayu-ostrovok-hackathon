"""ЗАГЛУШКА поиска для интерфейса: условия проверяются на уровне отеля по фактам v9.

Это не движок поиска. Настоящий (лид) проверяет условия уровня номера на каждом допустимом
размещении категории (D17) и пересчитывает после ответа отельера. Здесь — только честная
семантика статусов: оценка не закрывает обязательное (D18), неизвестное не становится «нет» (D5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.query_ast import Condition, QueryAST
from app.stub_store import StubStore
from core.facts import Fact, Status
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


def check(store: StubStore, hotel_id: str, c: Condition, hard: bool) -> dict[str, Any]:
    facts = [
        f
        for f in store.by_hotel.get(hotel_id, {}).get(c.feature_id, [])
        if _params_match(f, c.params)
    ]
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
    results = [_compare(c.op, _number(f, c.field), c.value) for f in usable]
    out["fact_ids"] = [f.fact_id for f in usable]
    out["statuses"] = sorted({f.status for f in usable})
    if any(r is False for r in results):
        return out | {"verdict": Verdict.no}
    if all(r is True for r in results):
        return out | {"verdict": Verdict.yes}
    return out | {"verdict": Verdict.unknown, "reason": "значение не сравнимо с условием"}


def search(store: StubStore, ast: QueryAST) -> list[dict[str, Any]]:
    results = []
    for hotel_id, hotel in store.hotels.items():
        if ast.city_id and hotel["city_id"] != ast.city_id:
            continue
        hard = [check(store, hotel_id, c, hard=True) for c in ast.hard]
        soft = [check(store, hotel_id, c, hard=False) | {"weight": c.weight} for c in ast.soft]
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
