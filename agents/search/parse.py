"""Разбор запроса гостя: текст → `QueryAST` (контракт 3).

Порядок (v4b §9): правила → если правила покрыли весь текст, модель не нужна → иначе LLM →
перевод в полный AST и проверка → при любой неудаче LLM — результат правил с пометкой
«офлайн». Неразобранное остаётся в `unparsed_spans`; ничего не теряется молча.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from agents.catalog import load_catalog
from agents.llm import Gateway, LLMError, LLMResult, get_gateway
from agents.query_ast import QueryAST
from agents.search.compact import CompactAST, compact_schema, to_full_ast
from agents.search.prompt import system_prompt, user_prompt
from agents.search.rules import parse_rules
from agents.search.text import normalize
from agents.search.validate import problems

log = logging.getLogger("agents.search")

NOTE_OFFLINE = "Запрос разобран правилами (офлайн-режим): ключ LLM не задан."
NOTE_LLM_FAILED = "Запрос разобран правилами (офлайн-режим): модель недоступна."
NOTE_LLM_INVALID = "Запрос разобран правилами: ответ модели не прошёл проверку."
NOTE_UNPARSED = "Часть запроса не разобрана — она показана отдельно и не считается выполненной."

CACHE_SIZE = 256
_cache: OrderedDict[tuple, QueryAST] = OrderedDict()


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

    def offline(note: str | None, errs: list[str] | None = None) -> ParseOutcome:
        notes = [note] if note else []
        if rules_ast.unparsed_spans:
            notes.append(NOTE_UNPARSED)
        return ParseOutcome(rules_ast, notes, problems=errs or rules_problems)

    gw = gateway or get_gateway()
    if mode == "rules":
        return offline(None)
    if not gw.available:  # пометка «офлайн» — даже если правила справились сами
        return offline(NOTE_OFFLINE)
    if mode == "auto" and not rules_ast.unparsed_spans and not rules_problems:
        return offline(None)
    key = (normalize(text).strip(), city_id, catalog.version, today.isoformat())
    if key in _cache:
        _cache.move_to_end(key)
        return ParseOutcome(_cache[key])
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
    ast = to_full_ast(done.value, text, catalog, parsed_by="llm", city_id=city_id)
    errs = problems(ast, catalog)
    if errs:
        log.warning("llm ast problems: %s", errs)
        return offline(NOTE_LLM_INVALID, errs)
    _cache[key] = ast
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    notes = [NOTE_UNPARSED] if ast.unparsed_spans else []
    return ParseOutcome(ast, notes, llm=done.result)
