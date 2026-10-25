"""Журнал «вне каталога» (D34; `unmatched_atom` в docs/design/contracts.md): что спрашивают,
а мы не знаем.

Событие — на каждую непокрытую часть запроса: часть вне каталога (с атомом разбора) или
неразобранный кусок. Частота — число разных запросов за окно, а не число событий: тот же
запрос, отправленный ещё раз, счёт не раздувает. Исходный текст запроса в журнал не идёт —
только псевдоним запроса и фрагмент после редактуры (почта, телефоны, длинные номера).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from agents.catalog import load_places
from agents.query_ast import QueryAST
from agents.search.text import normalize

Stratum = Literal["live", "review", "synthetic"]  # живой поток не смешиваем с отзывами и тестами
PLACE = "nearby_place:"
UNPARSED = "unparsed"

_REDACT = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # почта
    re.compile(r"\+?\d[\d\s()-]{7,}\d"),  # телефон, номер карты или документа
)


def redact(text: str) -> str:
    for pattern in _REDACT:
        text = pattern.sub("…", text)
    return text


def _key(text: str) -> str:
    return redact(" ".join(normalize(text).split()).strip(" .,;:!?"))


def query_id(raw_text: str, city_id: str | None) -> str:
    """Псевдоним запроса: тот же текст в том же городе — тот же запрос."""
    return hashlib.sha256(f"{city_id or ''}|{_key(raw_text)}".encode()).hexdigest()[:16]


def unmatched_events(
    ast: QueryAST, *, stratum: Stratum = "live", now: datetime | None = None
) -> list[dict[str, Any]]:
    """События журнала по разобранному запросу — строки таблицы `unmatched_atom`."""
    now = now or datetime.now(UTC)
    qid = query_id(ast.raw_text, ast.city_id)

    def event(atom_id: str, text: str, span, normalized: str, **extra: Any) -> dict[str, Any]:
        return {
            "event_id": uuid.uuid4().hex,
            "query_id": qid,
            "atom_id": atom_id,
            "source_stratum": stratum,
            "city_id": ast.city_id,
            "language": "ru",
            "span_text": redact(text),
            "span_start": span.start if span else 0,
            "span_end": span.end if span else 0,
            "normalized": normalized,
            "requiredness": "unspecified",
            "mapping_status": "unmatched",
            "nearest_contracts": [],
            "state": "new",
            "linked_feature_id": None,
            "created_at": now,
        } | extra

    events = [
        event(
            o.atom_id or "",
            o.text_span,
            o.span,
            o.normalized if o.normalized.startswith(PLACE) else _key(o.normalized),
            requiredness=o.requiredness,
            mapping_status=o.mapping_status,
            nearest_contracts=list(o.nearest_contracts),
        )
        for o in ast.out_of_catalog
    ]
    events += [
        event(f"{UNPARSED}{i}", s.text, s, _key(s.text)) for i, s in enumerate(ast.unparsed_spans)
    ]
    return [e for e in events if e["normalized"]]


def label(normalized: str) -> str:
    """Подпись для экрана админа: «рядом: итальянский ресторан или пиццерия»."""
    if normalized.startswith(PLACE):
        category = load_places().get(normalized.removeprefix(PLACE))
        return f"рядом: {category.label}" if category else normalized
    return normalized


def summarize(events: list[dict[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    """«Что спрашивают, а мы не знаем»: группы по смыслу, частота — число разных запросов."""
    groups: dict[str, dict[str, Any]] = {}
    for e in sorted(events, key=lambda e: e["created_at"]):
        g = groups.setdefault(
            e["normalized"],
            {
                "normalized": e["normalized"],
                "label": label(e["normalized"]),
                "queries": set(),
                "required_queries": set(),
                "unparsed": e["atom_id"].startswith(UNPARSED),
                "nearest_contracts": [],
                "cities": set(),
                "examples": [],
                "first_seen": e["created_at"],
            },
        )
        g["queries"].add(e["query_id"])
        if e["requiredness"] == "required":
            g["required_queries"].add(e["query_id"])
        g["nearest_contracts"] += [
            c for c in e["nearest_contracts"] if c not in g["nearest_contracts"]
        ]
        if e["city_id"]:
            g["cities"].add(e["city_id"])
        if e["span_text"] not in g["examples"] and len(g["examples"]) < 3:
            g["examples"].append(e["span_text"])
        g["last_seen"] = e["created_at"]
    rows = [
        g
        | {
            "queries": len(g["queries"]),
            "required_queries": len(g["required_queries"]),
            "cities": sorted(g["cities"]),
            "first_seen": g["first_seen"].isoformat(),
            "last_seen": g["last_seen"].isoformat(),
        }
        for g in groups.values()
    ]
    rows.sort(key=lambda r: (-r["queries"], -r["required_queries"], r["normalized"]))
    return rows[:limit]
