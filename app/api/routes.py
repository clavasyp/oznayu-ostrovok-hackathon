"""API на заглушках (контракт 4). Формы ответов — те, что будут у настоящего API (core.md §10).

Каждый ответ помечен `"stub": true`: данные настоящие (факты v9), но логика — временная.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agents.query_ast import QueryAST
from app import stub_search
from app.stub_store import StubStore
from core.verdict import BOOKED_LABEL, BOOKING_IMITATION_NOTE, GUEST_LABELS

router = APIRouter(prefix="/api")


def store(request: Request) -> StubStore:
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
    return {"stub": True, "cities": store(request).cities}


@router.get("/cities/{city_id}/coverage")
def coverage(city_id: str, request: Request) -> dict[str, Any]:
    rows = store(request).coverage.get(city_id)
    if rows is None:
        raise HTTPException(404, f"нет города {city_id}")
    return {"stub": True, "city_id": city_id, "features": rows}


@router.get("/hotels")
def hotels(request: Request, city_id: str | None = None) -> dict[str, Any]:
    items = [h for h in store(request).hotels.values() if not city_id or h["city_id"] == city_id]
    return {"stub": True, "hotels": items}


@router.get("/hotels/{hotel_id}")
def hotel(hotel_id: str, request: Request) -> dict[str, Any]:
    s = store(request)
    if hotel_id not in s.hotels:
        raise HTTPException(404, f"нет отеля {hotel_id} в заглушках")
    facts = {
        feature: [f.model_dump(mode="json") for f in items]
        for feature, items in sorted(s.by_hotel.get(hotel_id, {}).items())
    }
    return {"stub": True, "hotel": s.hotels[hotel_id], "facts": facts}


@router.get("/facts/{fact_id}")
def fact(fact_id: str, request: Request) -> dict[str, Any]:
    f = store(request).facts.get(fact_id)
    if f is None:
        raise HTTPException(404, f"нет факта {fact_id}")
    return {"stub": True, "fact": f.model_dump(mode="json")}


@router.post("/search")
def search(body: SearchRequest, request: Request) -> dict[str, Any]:
    notes = []
    ast = body.ast
    if ast is None:
        ast = stub_search.example_ast()
        notes.append(
            "Разбор текста — заглушка: используется пример запроса трека. "
            "Настоящий разбор — агент Данила."
        )
    if body.city_id:
        ast = ast.model_copy(update={"city_id": body.city_id})
    return {
        "stub": True,
        "notes": notes,
        "ast": ast.model_dump(mode="json", by_alias=True),
        "results": stub_search.search(store(request), ast),
    }


@router.get("/hotelier/{hotel_id}/questions")
def hotelier_questions(hotel_id: str, request: Request) -> dict[str, Any]:
    return {
        "stub": True,
        "hotel_id": hotel_id,
        "questions": store(request).questions.get(hotel_id, []),
    }


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
    items = [c for c in store(request).conflicts if not city_id or c["city_id"] == city_id]
    return {"stub": True, "conflicts": items}
