"""Обратное условие того же смысла — для «не / без» в правилах и `negated_atoms` от модели.

«Без собак» → species in [cat, none]; «не на первом этаже» → floor ne 1; «без балкона» →
private_balcony = false; «не дальше» ↔ «дальше». Если обратное так не выразить — None: кусок
остаётся непонятым или уходит в уточнение, но не становится противоположным условием.
"""

from __future__ import annotations

from agents.catalog import Feature

FLIP = {"lte": "gt", "gt": "lte", "gte": "lt", "lt": "gte", "eq": "ne", "ne": "eq"}


def negate(feature: Feature, field: str | None, op: str, value: str) -> tuple[str, str] | None:
    """(оператор, значение строкой) обратного условия или None."""
    spec = feature.field_spec(field or None)
    op = op or feature.ops[0]
    if op == "in":
        values = {v.strip() for v in value.split(",")}
        rest = [v for v in (spec.values if spec else ()) if v not in values]
        return ("in", ",".join(rest)) if rest else None
    is_bool = spec is None or spec.type == "bool"
    if op == "eq" and is_bool and value.strip().lower() in ("true", "false", ""):
        return "eq", "false" if value.strip().lower() in ("true", "") else "true"
    if op in FLIP:
        return FLIP[op], value
    return None
