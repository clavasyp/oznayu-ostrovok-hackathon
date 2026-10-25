"""API (контракт 4, core.md §10). Источник — `core.store.Store`: база (`DATA_MODE=db`) или
заглушки из фактов v9 (`DATA_MODE=fixtures`). Флаг `"stub"` в ответе говорит, какой из них.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from agents import hotelier as agent
from agents.query_ast import QueryAST
from agents.search import parse_query
from core import search as engine
from core.facts import Fact
from core.hotelier import AnswerError, AnswerIn, build_answer
from core.reasons import reason_text
from core.store import Store
from core.verdict import BOOKED_LABEL, BOOKING_IMITATION_NOTE, GUEST_LABELS

router = APIRouter(prefix="/api")
SEARCH_LEVEL_NOTE = (
    "Условия уровня окна (вид, солнце, шум у окна) проверяются по размещениям категорий "
    "«здание × этаж × окна» (D17) — все вместе на одном размещении; остальные — по отелю, "
    "корпусам и маршрутам."
)


def fact_json(f: Fact) -> dict[str, Any]:
    out = f.model_dump(mode="json")
    if f.unknown_reason:
        out["unknown_reason_text"] = reason_text(f.unknown_reason)
    return out


def store(request: Request) -> Store:
    return request.app.state.store


class SearchRequest(BaseModel):
    text: str | None = None
    city_id: str | None = None
    ast: QueryAST | None = None


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
    }


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
    facts = {
        feature: [fact_json(f) for f in items] for feature, items in s.hotel_facts(hotel_id).items()
    }
    room_types = s.room_types([hotel_id]).get(hotel_id, [])
    return {"stub": s.stub, "hotel": h, "facts": facts, "room_types": room_types}


@router.get("/facts/{fact_id}")
def fact(fact_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    f = s.fact(fact_id)
    if f is None:
        raise HTTPException(404, f"нет факта {fact_id}")
    return {"stub": s.stub, "fact": fact_json(f)}


@router.post("/search")
def search(body: SearchRequest, request: Request) -> dict[str, Any]:
    s = store(request)
    notes = [SEARCH_LEVEL_NOTE]
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
    return {
        "stub": s.stub,
        "notes": notes,
        "ast": ast.model_dump(mode="json", by_alias=True),
        "results": engine.search(s, ast),
    }


@router.get("/hotelier/{hotel_id}/questions")
def hotelier_questions(hotel_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    # Агент добавляет к вопросам текст предзаполнения Островка и поля ответа (новые ключи).
    questions = [agent.enrich_question(q) for q in s.questions(hotel_id)]
    return {"stub": s.stub, "hotel_id": hotel_id, "questions": questions}


@router.post("/hotelier/{hotel_id}/simulate")
def hotelier_simulate(hotel_id: str, body: SimulateRequest, request: Request) -> dict[str, Any]:
    """Свободный ответ имитатора отельера по закрытому профилю (D31: всегда `is_simulated`).

    Ответ не сохраняется: его отправляют в `…/answers` с `is_simulated: true`.
    """
    q = store(request).question(hotel_id, body.question_id, body.feature_id)
    if q is None:
        raise HTTPException(404, f"нет такого вопроса у отеля {hotel_id}")
    return {"question_id": q["question_id"]} | agent.simulate_answer(
        hotel_id, q["feature_id"], seed=body.seed
    )


@router.post("/hotelier/{hotel_id}/questions/{question_id}/ask")
def hotelier_ask(hotel_id: str, question_id: int, request: Request) -> dict[str, Any]:
    """Вопрос отправлен отелю (кабинет отельера: «спросили»)."""
    s = store(request)
    if s.question(hotel_id, question_id, None) is None:
        raise HTTPException(404, f"нет вопроса {question_id} у отеля {hotel_id}")
    s.set_question_state(question_id, "asked", datetime.now(UTC))
    return {"stub": s.stub, "question": s.question(hotel_id, question_id, None)}


def _hotel_view(s: Store, ast: QueryAST, hotel_id: str) -> dict[str, Any] | None:
    found = engine.search(s, ast.model_copy(update={"city_id": None}), only=[hotel_id])
    if not found:
        return None
    r = found[0]
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
        "question": s.question(hotel_id, result.question_id, None),
        "facts": [fact_json(f) for f in result.facts],
        "superseded": result.superseded,
        "conflicts": [{"fact_a": a.fact_id, "fact_b": b.fact_id} for a, b in result.conflicts],
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
