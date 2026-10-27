"""API (контракт 4, core.md §10). Источник — `core.store.Store`: база (`DATA_MODE=db`) или
заглушки из фактов v9 (`DATA_MODE=fixtures`). Флаг `"stub"` в ответе говорит, какой из них.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from agents import hotelier as agent
from agents.catalog import load_catalog
from agents.explain import explain
from agents.hotelier.schemas import label as field_label
from agents.query_ast import QueryAST
from agents.search import parse_query
from agents.search.journal import summarize, unmatched_events
from core import search as engine
from core.facts import Fact, Status
from core.hotelier import AnswerError, AnswerIn, build_answer
from core.reasons import reason_text
from core.store import Store
from core.verdict import BOOKED_LABEL, BOOKING_IMITATION_NOTE, GUEST_LABELS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
# Объяснение по шаблону — у первых результатов (у остальных — по запросу, POST /api/explain).
EXPLAIN_TOP = 20
SEARCH_LEVEL_NOTE = (
    "Условия уровня окна (вид, солнце, шум у окна) проверяются по размещениям категорий "
    "«здание × этаж × окна» (D17) — все вместе на одном размещении; остальные — по отелю, "
    "корпусам и маршрутам."
)
NEGATION = re.compile(r"(?:не|без|нет|никаких)\b")


def fact_json(f: Fact) -> dict[str, Any]:
    out = f.model_dump(mode="json")
    if f.unknown_reason:
        out["unknown_reason_text"] = reason_text(f.unknown_reason)
    return str_question_ids(out)  # паспорт ответа отеля: id вопроса — строкой


def str_question_ids(x: Any) -> Any:
    """`question_id` строкой на любой глубине ответа: паспорта фактов и размещений тоже несут
    id вопроса (см. `question_json`)."""
    if isinstance(x, dict):
        return {
            k: str(v) if k == "question_id" and isinstance(v, int) else str_question_ids(v)
            for k, v in x.items()
        }
    if isinstance(x, list):
        return [str_question_ids(v) for v in x]
    return x


def question_json(q: dict[str, Any]) -> dict[str, Any]:
    """`question_id` строкой: id вопросов 60-битные (`core.packs.stable_id`), а число в JSON
    браузер читает как double и округляет после 2^53 — «отправить вопрос» били бы мимо (404).
    На вход API принимает и строку, и число."""
    return q | {"question_id": str(q["question_id"])}


def store(request: Request) -> Store:
    return request.app.state.store


class SearchRequest(BaseModel):
    text: str | None = None
    city_id: str | None = None
    ast: QueryAST | None = None
    # Страница выдачи: с полным каталогом в городе тысячи отелей (`total` — сколько всего).
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class SimulateRequest(BaseModel):
    question_id: int | None = None
    feature_id: str | None = None
    seed: int | None = None


@router.get("/meta/labels")
def labels() -> dict[str, Any]:
    """Слова на экранах (D31) — из одного места, чтобы интерфейс и API не расходились."""
    return {
        "statuses": {k.value: v for k, v in GUEST_LABELS.items()},
        "booked": BOOKED_LABEL,
        "booking_imitation_note": BOOKING_IMITATION_NOTE,
        "out_of_catalog": "Такое мы пока не знаем",
        # Названия характеристик (каталог агентов): интерфейсу не нужен свой словарь.
        "features": {f.id: f.short for f in load_catalog().features.values()},
    }


def _label_items(items: list[dict[str, Any]], conds: dict[str, Any]) -> None:
    """Подписи пунктов проверки для экрана: `label` — словами гостя, `feature_name` — название
    характеристики («можно с животным (масса)»).

    Рядом с вердиктом показываем слова гостя: «без собак» правила переворачивают в «можно только
    с кошкой или без животных», и «можно с животным: нет» читалось бы наоборот. Так же у группы
    «ИЛИ» (её `text_span` — варианты через «или») и у «НЕ» (`negated`)."""
    catalog = load_catalog()
    for item in items:
        feature = catalog.get(item.get("feature_id") or "")
        cond = conds.get(item.get("atom_id") or "")
        # Название — под направление условия: «бары и клубы рядом» у «≥ 2», «мало баров…» у «≤ 2».
        name = (
            (feature.short_for(cond.op, cond.value) if cond else feature.short) if feature else None
        )
        field = cond.field if cond else None
        if feature and field and field != feature.default_field:
            name = f"{name} ({field_label(field).split(',')[0]})"
        span = (item.get("text_span") or "").strip()
        if item.get("negated") and span and not NEGATION.match(span.lower()):
            span = f"не {span}"
        item["feature_name"] = name
        item["label"] = span or name or item.get("feature_id") or "условие"
        if item.get("items"):
            _label_items(item["items"], conds)


def _label_result(r: dict[str, Any], ast: QueryAST) -> None:
    conds = {c.atom_id: c for c in [*ast.hard, *ast.soft] if c.atom_id}
    _label_items(r.get("hard", []), conds)
    _label_items(r.get("soft", []), conds)
    for cat in r.get("categories", []):
        _label_items(cat.get("conditions", []), conds)


@router.get("/cities")
def cities(request: Request) -> dict[str, Any]:
    s = store(request)
    return {"stub": s.stub, "cities": s.cities()}


@router.get("/cities/{city_id}/coverage")
def coverage(city_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    rows = s.coverage(city_id)
    if rows is None:
        raise HTTPException(404, f"нет города {city_id}")
    return {"stub": s.stub, "city_id": city_id, "features": rows}


@router.get("/hotels")
def hotels(request: Request, city_id: str | None = None) -> dict[str, Any]:
    s = store(request)
    return {"stub": s.stub, "hotels": s.hotels(city_id)}


@router.get("/hotels/{hotel_id}")
def hotel(hotel_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    h = s.hotel(hotel_id)
    if h is None:
        raise HTTPException(404, f"нет отеля {hotel_id}")
    facts: dict[str, list[dict[str, Any]]] = {}
    windows: dict[str, dict[str, int]] = {}
    for feature, items in s.hotel_facts(hotel_id).items():
        # Факты окон (вид и солнце — тысячи на отель) — счётчиками; каждый — по /facts/{id}.
        plain = [f for f in items if f.subject_level not in engine.WINDOW_LEVELS]
        if plain:
            facts[feature] = [fact_json(f) for f in plain]
        if len(plain) < len(items):
            windows[feature] = _window_counts(
                [f for f in items if f.subject_level in engine.WINDOW_LEVELS]
            )
    room_types = str_question_ids(s.room_types([hotel_id]).get(hotel_id, []))  # паспорт размещений
    return {
        "stub": s.stub,
        "hotel": h,
        "facts": facts,
        "windows": windows,
        "room_types": room_types,
    }


def _window_counts(items: list[Fact]) -> dict[str, int]:
    """Факты окон одной характеристики: сколько «да» и «нет» по расчёту, по оценке, неизвестно."""
    out = {"facts": len(items), "computed_yes": 0, "computed_no": 0, "estimate_yes": 0,
           "estimate_no": 0, "unknown": 0}  # fmt: skip
    for f in items:
        visible = (f.value or {}).get("visible") if isinstance(f.value, dict) else f.value
        if f.status == Status.estimate:
            out["estimate_yes" if visible else "estimate_no"] += 1
        elif f.status == Status.unknown or visible is None:
            out["unknown"] += 1
        else:
            out["computed_yes" if visible else "computed_no"] += 1
    return out


@router.get("/facts/{fact_id}")
def fact(fact_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    f = s.fact(fact_id)
    if f is None:
        raise HTTPException(404, f"нет факта {fact_id}")
    return {"stub": s.stub, "fact": fact_json(f)}


def _query_ast(body: SearchRequest, notes: list[str]) -> QueryAST:
    ast = body.ast
    if ast is None and body.text and body.text.strip():
        # Разбор текста (агент-поисковик): правила, модель — для остатка, без ключа — офлайн.
        outcome = parse_query(body.text, body.city_id)
        ast = outcome.ast
        notes += outcome.notes
    elif ast is None:
        ast = engine.example_ast()
        notes.append(
            "Разбор текста — заглушка: используется пример запроса трека. "
            "Настоящий разбор — агент Данила."
        )
    if body.city_id:
        ast = ast.model_copy(update={"city_id": body.city_id})
    return ast


@router.post("/search")
def search(body: SearchRequest, request: Request) -> dict[str, Any]:
    s = store(request)
    notes = [SEARCH_LEVEL_NOTE]
    ast = _query_ast(body, notes)
    if body.ast is None and body.text and body.text.strip():
        _record_unmatched(s, ast)  # журнал «вне каталога» — только по тексту, который разобрали мы
    results = engine.search(s, ast)
    counts = Counter(str(r["status"]) for r in results)
    page = results[body.offset : body.offset + body.limit]  # полный каталог — тысячи отелей
    # Объяснение по шаблону (без модели, мгновенно) — у первых результатов страницы.
    for r in page[:EXPLAIN_TOP]:
        r["explanation"] = explain(r, s.fact, ast=ast).as_dict()
    for r in page:
        _label_result(r, ast)
    return {
        "stub": s.stub,
        "notes": notes,
        "ast": ast.model_dump(mode="json", by_alias=True),
        "total": len(results),
        "counts": {k: counts.get(k, 0) for k in ("fits", "needs_check", "not_fit")},
        "offset": body.offset,
        "limit": body.limit,
        "results": page,
    }


class ExplainRequest(SearchRequest):
    hotel_id: str


@router.post("/explain")
def explain_hotel(body: ExplainRequest, request: Request) -> dict[str, Any]:
    """Объяснение по одному отелю: порядок и связки от модели, цифры — из фактов (D8).

    Без ключа, при сбое модели или проверки — то же объяснение по шаблону.
    """
    s = store(request)
    notes: list[str] = []
    ast = _query_ast(body, notes).model_copy(update={"city_id": None})
    found = engine.search(s, ast, only=[body.hotel_id])
    if not found:
        raise HTTPException(404, f"нет отеля {body.hotel_id}")
    r = found[0]
    e = explain(r, s.fact, ast=ast, use_llm=True)
    return {
        "stub": s.stub,
        "hotel_id": body.hotel_id,
        "status": r["status"],
        "status_label": r["status_label"],
        "explanation": e.as_dict(),
        "notes": notes + e.notes,
    }


def _record_unmatched(s: Store, ast: QueryAST) -> None:
    """Сбой записи журнала поиск не ломает (D34: журнал — для админа, а не для гостя)."""
    try:
        s.record_unmatched(unmatched_events(ast))
    except Exception:
        log.exception("журнал «вне каталога»: запись не удалась")


@router.get("/hotelier/{hotel_id}/questions")
def hotelier_questions(hotel_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    # Агент добавляет к вопросам текст предзаполнения Островка и поля ответа (новые ключи).
    questions = [question_json(agent.enrich_question(q)) for q in s.questions(hotel_id)]
    return {"stub": s.stub, "hotel_id": hotel_id, "questions": questions}


@router.post("/hotelier/{hotel_id}/simulate")
def hotelier_simulate(hotel_id: str, body: SimulateRequest, request: Request) -> dict[str, Any]:
    """Свободный ответ имитатора отельера по закрытому профилю (D31: всегда `is_simulated`).

    Ответ не сохраняется: его отправляют в `…/answers` с `is_simulated: true`.
    """
    q = store(request).question(hotel_id, body.question_id, body.feature_id)
    if q is None:
        raise HTTPException(404, f"нет такого вопроса у отеля {hotel_id}")
    return {"question_id": str(q["question_id"])} | agent.simulate_answer(
        hotel_id, q["feature_id"], seed=body.seed
    )


@router.post("/hotelier/{hotel_id}/questions/{question_id}/ask")
def hotelier_ask(hotel_id: str, question_id: int, request: Request) -> dict[str, Any]:
    """Вопрос отправлен отелю (кабинет отельера: «спросили»)."""
    s = store(request)
    if s.question(hotel_id, question_id, None) is None:
        raise HTTPException(404, f"нет вопроса {question_id} у отеля {hotel_id}")
    s.set_question_state(question_id, "asked", datetime.now(UTC))
    return {"stub": s.stub, "question": _question(s, hotel_id, question_id)}


def _question(s: Store, hotel_id: str, question_id: int) -> dict[str, Any] | None:
    """Вопрос как в списке вопросов — с предзаполнением Островка и полями ответа (агент)."""
    q = s.question(hotel_id, question_id, None)
    return question_json(agent.enrich_question(q)) if q else None


def _hotel_view(s: Store, ast: QueryAST, hotel_id: str) -> dict[str, Any] | None:
    found = engine.search(s, ast.model_copy(update={"city_id": None}), only=[hotel_id])
    if not found:
        return None
    r = found[0]
    _label_result(r, ast)
    return {k: r[k] for k in ("status", "status_label", "hard", "best_category")}


@router.post("/hotelier/{hotel_id}/answers")
def hotelier_answer(hotel_id: str, body: AnswerIn, request: Request) -> dict[str, Any]:
    """Ответ отеля → факты с паспортом → поиск сразу видит новое (core.hotelier).

    С `ast` в теле ответ показывает статус отеля по этому запросу до и после — так видно,
    как ответ меняет выдачу.
    """
    s = store(request)
    q = s.question(hotel_id, body.question_id, body.feature_id)
    if q is None:
        raise HTTPException(404, f"нет такого вопроса у отеля {hotel_id}")
    existing = s.facts_for(q["feature_id"], [hotel_id]).get(hotel_id, [])
    parsed, agent_note = None, None
    if body.answer_text and not (body.fields or body.dont_know or body.placements):
        # Только текст: агент-отельер разбирает его в поля вопроса (без догадок, с цитатами).
        # Не смог — текст сохраняется без полей (answered_unparsed), а не 500.
        try:
            parsed = agent.answer_from_text(q, body.answer_text)
            body = AnswerIn.model_validate(body.model_dump() | parsed.body)
        except (ValueError, ValidationError) as e:
            agent_note = f"Агент не разобрал ответ ({type(e).__name__}): текст сохранён без полей."
    try:
        result = build_answer(q, body, existing, datetime.now(UTC))
    except AnswerError as e:
        raise HTTPException(422, str(e)) from e
    before = _hotel_view(s, body.ast, hotel_id) if body.ast else None
    try:
        s.apply_answer(result)
    except AnswerError as e:
        raise HTTPException(422, str(e)) from e
    after = _hotel_view(s, body.ast, hotel_id) if body.ast else None
    notes = []
    if agent_note:
        notes.append(agent_note)
    elif result.state == "answered_unparsed" and parsed is None:
        notes.append("Текст сохранён без полей: его разберёт агент-отельер, факт появится после.")
    elif result.state == "answered_unparsed":
        notes.append("Агент не нашёл в ответе полей вопроса — факта нет, нужно уточнение.")
    if parsed is not None:
        notes += parsed.report["notes"]
    if body.is_simulated:
        notes.append("Ответ имитатора: факт помечен simulated_hotelier и показывается с пометкой.")
    return {
        "stub": s.stub,
        "state": result.state,
        "question": _question(s, hotel_id, result.question_id),
        "facts": [fact_json(f) for f in result.facts],
        "superseded": result.superseded,
        "conflicts": [{"fact_a": a.fact_id, "fact_b": b.fact_id} for a, b in result.conflicts],
        # Расчёт и ответ разошлись: вопрос отельеру с обоими значениями (победителя не назначаем).
        "conflict_questions": [agent.conflict_question(a, b) for a, b in result.conflicts],
        "placements_saved": result.placements is not None,
        "before": before,
        "after": after,
        "notes": notes,
        # Что понял агент: поля с цитатами, отклонённое с причинами, уточняющий вопрос.
        "agent": parsed.report if parsed else None,
    }


@router.get("/conflicts")
def conflicts(request: Request, city_id: str | None = None) -> dict[str, Any]:
    """Сверка с карточкой Островка (`conflicts`) и противоречия между фактами (`fact_conflicts`)."""
    s = store(request)
    return {
        "stub": s.stub,
        "conflicts": s.conflicts(city_id),
        "fact_conflicts": s.fact_conflicts(city_id),
    }


@router.get("/out-of-catalog")
def out_of_catalog(
    request: Request,
    city_id: str | None = None,
    stratum: str = "live",
    days: int = 30,
    limit: int = 50,
) -> dict[str, Any]:
    """«Что спрашивают, а мы не знаем» (экран админа, D34): части запросов вне каталога и
    неразобранные куски. Частота — число разных запросов за `days` дней, а не число событий."""
    s = store(request)
    since = datetime.now(UTC) - timedelta(days=days)
    events = s.unmatched_events(city_id=city_id, stratum=stratum, since=since)
    return {
        "stub": s.stub,
        "city_id": city_id,
        "stratum": stratum,
        "window_days": days,
        "items": summarize(events, limit=limit),
    }
