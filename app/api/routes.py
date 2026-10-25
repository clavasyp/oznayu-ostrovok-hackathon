"""API (контракт 4, core.md §10). Источник — `core.store.Store`: база (`DATA_MODE=db`) или
заглушки из фактов v9 (`DATA_MODE=fixtures`). Флаг `"stub"` в ответе говорит, какой из них.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.query_ast import QueryAST
from core import search as engine
from core.store import Store
from core.verdict import BOOKED_LABEL, BOOKING_IMITATION_NOTE, GUEST_LABELS

router = APIRouter(prefix="/api")
SEARCH_LEVEL_NOTE = (
    "Условия проверяются на уровне отеля, его корпусов и маршрутов. "
    "Размещения категорий (этаж × окна, D17) — следующим шагом."
)


def store(request: Request) -> Store:
    return request.app.state.store


class SearchRequest(BaseModel):
    text: str | None = None
    city_id: str | None = None
    ast: QueryAST | None = None


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
        feature: [f.model_dump(mode="json") for f in items]
        for feature, items in s.hotel_facts(hotel_id).items()
    }
    return {"stub": s.stub, "hotel": h, "facts": facts}


@router.get("/facts/{fact_id}")
def fact(fact_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    f = s.fact(fact_id)
    if f is None:
        raise HTTPException(404, f"нет факта {fact_id}")
    return {"stub": s.stub, "fact": f.model_dump(mode="json")}


@router.post("/search")
def search(body: SearchRequest, request: Request) -> dict[str, Any]:
    s = store(request)
    notes = [SEARCH_LEVEL_NOTE]
    ast = body.ast
    if ast is None:
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
    return {"stub": s.stub, "hotel_id": hotel_id, "questions": s.questions(hotel_id)}


@router.post("/hotelier/{hotel_id}/answers", status_code=202)
def hotelier_answer(hotel_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "stub": True,
        "accepted": False,
        "reason": "Разбор ответа по полям — агент-отельер (Данил). Пока ответ не сохраняется.",
        "echo": body,
    }


@router.get("/conflicts")
def conflicts(request: Request, city_id: str | None = None) -> dict[str, Any]:
    s = store(request)
    return {"stub": s.stub, "conflicts": s.conflicts(city_id)}
