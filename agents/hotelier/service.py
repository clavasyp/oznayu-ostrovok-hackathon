"""Агент-отельер целиком: анкета → ответ (имитатора или отельера) → поля → факт → выдача.

Хранилище — любой объект с `hotels`, `questions`, `conflicts` и `add_fact(fact)` (сейчас —
заглушка `app.stub_store.StubStore`, потом — база). Новый факт сразу виден поиску.
"""

from __future__ import annotations

from typing import Any, Protocol

from agents.catalog import load_catalog
from agents.hotelier.parse_answer import parse_answer
from agents.hotelier.placement import parse_placement, placement_fact
from agents.hotelier.questions import placement_question, question_id, questionnaire
from agents.hotelier.simulator import load_profile
from agents.hotelier.to_fact import answer_fact, conflicts_with_ostrovok
from agents.llm import Gateway
from core.facts import Fact


class Store(Protocol):
    hotels: dict[str, dict[str, Any]]
    questions: dict[str, list[dict[str, Any]]]
    conflicts: list[dict[str, Any]]

    def add_fact(self, fact: Fact) -> None: ...


def hotel_questions(store: Store, hotel_id: str) -> list[dict[str, Any]]:
    """26 вопросов анкеты v9 с предзаполнением Островка."""
    return questionnaire(hotel_id, store.questions.get(hotel_id, []))


def placement_questions(hotel_id: str) -> list[dict[str, Any]]:
    """Адресные вопросы «стороны и этажи окон» по категориям отеля.

    Категории пока берутся из профиля имитатора; после коннектора Островка (Сева) — из `room_type`.
    """
    return [
        placement_question(hotel_id, c)
        for c in (load_profile(hotel_id) or {}).get("categories", [])
    ]


def submit_answer(
    store: Store,
    hotel_id: str,
    feature_id: str,
    answer_text: str,
    *,
    is_simulated: bool,
    room_type_id: str | None = None,
    gateway: Gateway | None = None,
) -> dict[str, Any]:
    """Разобрать ответ, сохранить факт и противоречия. Без догадок: неясное — уточнение."""
    hotel = store.hotels.get(hotel_id)
    if hotel is None:
        raise KeyError(hotel_id)
    if feature_id == "category_placement":
        return _submit_placement(store, hotel, room_type_id, answer_text, is_simulated, gateway)
    feature = load_catalog().get(feature_id)
    if feature is None or feature.source != "hotelier":
        raise ValueError(f"{feature_id} — не вопрос отельеру")
    raw = next((q for q in store.questions.get(hotel_id, []) if q["feature_id"] == feature_id), {})
    parsed = parse_answer(feature_id, answer_text, raw.get("question", ""), gateway=gateway)
    qid = question_id(hotel_id, feature_id)
    fact = answer_fact(
        hotel_id=hotel_id,
        city_id=hotel["city_id"],
        feature=feature,
        parsed=parsed,
        question_id=qid,
        is_simulated=is_simulated,
        room_type_id=room_type_id,
    )
    conflicts: list[dict[str, Any]] = []
    if fact is not None:
        conflicts = conflicts_with_ostrovok(fact, raw.get("claims") or [], hotel.get("name"))
        if conflicts:
            # Противоречие не решается в пользу последнего ответа (core §3): пока оно открыто,
            # обязательное условие по признаку — «Нужно уточнить».
            fact = fact.model_copy(update={"hard_condition_eligible": False})
        store.add_fact(fact)
        store.conflicts.extend(conflicts)
    return {
        "question_id": qid,
        "hotel_id": hotel_id,
        "feature_id": feature_id,
        "is_simulated": is_simulated,
        "answer_state": parsed.answer_state,
        "parsed_by": parsed.parsed_by,
        "fields": {
            name: {"value": a.value, "quote": a.quote, "span": [a.start, a.end]}
            for name, a in parsed.accepted.items()
        },
        "rejected": parsed.rejected,
        "followup": parsed.followup,
        "fact": fact.model_dump(mode="json") if fact else None,
        "conflicts": conflicts,
        "notes": parsed.notes,
    }


def _submit_placement(
    store: Store,
    hotel: dict[str, Any],
    room_type_id: str | None,
    answer_text: str,
    is_simulated: bool,
    gateway: Gateway | None,
) -> dict[str, Any]:
    if not room_type_id:
        raise ValueError("для размещения нужна категория room_type_id")
    hotel_id = hotel["hotel_id"]
    qid = question_id(hotel_id, "category_placement", room_type_id)
    compact, rejected, parsed_by = parse_placement(answer_text, gateway=gateway)
    fact = placement_fact(
        hotel_id=hotel_id,
        city_id=hotel["city_id"],
        room_type_id=room_type_id,
        compact=compact,
        question_id=qid,
        is_simulated=is_simulated,
        parsed_by=parsed_by,
    )
    if fact is not None:
        store.add_fact(fact)
    followup = None
    if not compact.placements:
        followup = "Уточните, пожалуйста: на какую сторону выходят окна и с каких по какие этажи?"
    elif compact.guarantee == "unknown":
        followup = "Можно ли закрепить этот вариант (сторону и этаж) при бронировании?"
    return {
        "question_id": qid,
        "hotel_id": hotel_id,
        "feature_id": "category_placement",
        "room_type_id": room_type_id,
        "is_simulated": is_simulated,
        "answer_state": "answered" if compact.placements else "unknown",
        "parsed_by": parsed_by,
        "fields": compact.model_dump(),
        "rejected": rejected,
        "followup": followup,
        "fact": fact.model_dump(mode="json") if fact else None,
        "conflicts": [],
        "notes": [],
    }
