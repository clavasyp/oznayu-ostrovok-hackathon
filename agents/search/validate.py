"""Проверка разобранного запроса поверх схемы `QueryAST`.

Схема проверяет типы; здесь — смысл: характеристики и поля существуют, фрагменты совпадают
с текстом, уровень номера привязан к одному размещению (D17), веса мягких условий сходятся,
атомы не повторяются и не теряются, граф логики ссылается на существующие узлы.
"""

from __future__ import annotations

from agents.catalog import Catalog
from agents.query_ast import QueryAST


def problems(ast: QueryAST, catalog: Catalog) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def atom(atom_id: str | None, where: str) -> None:
        if not atom_id:
            out.append(f"{where}: нет atom_id")
        elif atom_id in seen:
            out.append(f"{where}: atom_id {atom_id} повторяется")
        else:
            seen.add(atom_id)

    def span(s, where: str) -> None:
        if s is None:
            out.append(f"{where}: нет позиции во фрагменте запроса")
        elif (
            not (0 <= s.start < s.end <= len(ast.raw_text))
            or ast.raw_text[s.start : s.end] != s.text
        ):
            out.append(f"{where}: позиция {s.start}–{s.end} не совпадает с текстом")

    for kind, conds in (("hard", ast.hard), ("soft", ast.soft)):
        for i, c in enumerate(conds):
            where = f"{kind}[{i}] {c.feature_id}"
            atom(c.atom_id, where)
            span(c.span, where)
            feature = catalog.get(c.feature_id)
            if feature is None:
                out.append(f"{where}: нет такой характеристики")
                continue
            if feature.fields and c.field is not None and c.field not in feature.fields:
                out.append(f"{where}: нет поля {c.field}")
            if feature.room_level and (c.binding != "same_placement" or c.quantifier != "all"):
                out.append(f"{where}: уровень номера требует same_placement и all")
            if kind == "hard" and c.provenance != "explicit":
                out.append(f"{where}: выведенное условие не может быть обязательным")
    for i, o in enumerate(ast.out_of_catalog):
        where = f"out_of_catalog[{i}]"
        # Частичное обязательное — тот же атом, что и мягкое условие: повтор допустим.
        if o.mapping_status != "partial":
            atom(o.atom_id, where)
        span(o.span, where)
    if ast.soft:
        total = sum(c.weight for c in ast.soft)
        if abs(total - 1) > 0.01:
            out.append(f"веса мягких условий в сумме {total:.3f}, а не 1")
    for s in ast.unparsed_spans:
        span(s, "unparsed_spans")
    if ast.logic:
        ids = {n.node_id for n in ast.logic}
        hard_ids = {c.atom_id for c in ast.hard}
        for n in ast.logic:
            if n.op == "ATOM" and n.atom_id not in hard_ids:
                out.append(f"logic {n.node_id}: нет обязательного атома {n.atom_id}")
            if n.op != "ATOM" and (not n.children or any(c not in ids for c in n.children)):
                out.append(f"logic {n.node_id}: дети не найдены")
        if ast.hard_root not in ids:
            out.append("logic: нет корня hard_root")
    return out
