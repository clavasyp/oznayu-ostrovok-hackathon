"""Ответ отельера → факты с паспортом (core §7.2, D8, D31).

Свободный текст ответа в поля вопроса разбирает агент Данила (`agents/`). Здесь — правила,
по которым поля становятся фактами:
- статус `hotelier`, у ответа имитатора — `simulated_hotelier` (с пометкой, D31);
- значение факта — поля ответа ровно с именами из вопроса; лишние поля — ошибка, а не догадка;
- «не знаю» — факт `unknown` с причиной, а не «нет» (D5);
- новое заявление отеля заменяет его прежние заявления о том же месте: данные Островка (D26) и
  прошлые ответы. Имитатор не заменяет настоящий ответ;
- наш расчёт ответ не заменяет: если значения расходятся, это противоречие (core §6) —
  поиск покажет «нужно уточнить», а пара фактов попадёт в журнал противоречий;
- ответ про этажи и стороны номеров категории — размещения D17 (явные пары «здание × этаж × окна»).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agents.query_ast import QueryAST
from core.facts import Fact, Status, make_fact_id

ALGORITHM = "hotelier-answer.1"
HOTEL_ORIGIN = frozenset({Status.ostrovok_content, Status.hotelier, Status.simulated_hotelier})
SUPERSEDES = {
    Status.hotelier: HOTEL_ORIGIN,
    Status.simulated_hotelier: frozenset({Status.ostrovok_content, Status.simulated_hotelier}),
}
COMPARABLE = frozenset({Status.computed, Status.observed, Status.curated})


class AnswerError(ValueError):
    """Ответ нельзя принять: нет вопроса, лишние поля, нет категории."""


class PlacementIn(BaseModel):
    building_id: str
    floor: int
    floor_label: str | None = None
    building_part_id: str | None = None
    windows: list[str] = Field(min_length=1, description="ID фасадов; у углового номера несколько")


class PlacementsIn(BaseModel):
    completeness: Literal["complete", "partial", "unknown"]
    guarantee: Literal["all_guaranteed", "exact_room", "some_possible", "on_request"]
    items: list[PlacementIn] = Field(min_length=1)


class AnswerIn(BaseModel):
    """Ответ на вопрос. Поля — уже разобранные (агент Данила или кабинет отельера)."""

    question_id: int | None = None
    feature_id: str | None = None  # если нет question_id
    answer_text: str | None = None
    fields: dict[str, Any] | None = None
    # Параметры характеристики, к которым относится ответ (радиус, даты, масса питомца…):
    # сравнивается и заменяется только факт того же смысла (D15).
    params: dict[str, Any] = Field(default_factory=dict)
    dont_know: bool = False
    room_type_id: str | None = None  # ответ про конкретную категорию
    placements: PlacementsIn | None = None  # этажи и стороны номеров категории (D17)
    is_simulated: bool = False
    respondent: str | None = None
    ast: QueryAST | None = None  # показать статус отеля до и после ответа

    @model_validator(mode="after")
    def _has_question_and_content(self) -> AnswerIn:
        if self.question_id is None and not self.feature_id:
            raise ValueError("нужен question_id или feature_id")
        if not (self.fields or self.dont_know or self.placements or self.answer_text):
            raise ValueError("пустой ответ: нужны fields, dont_know, placements или answer_text")
        if self.placements and not self.room_type_id:
            raise ValueError("размещения относятся к категории: нужен room_type_id")
        return self


@dataclass
class AnswerResult:
    question_id: int
    hotel_id: str
    feature_id: str
    state: str  # answered | answered_unknown | answered_unparsed
    answer_text: str | None
    parsed: dict[str, Any] | None
    is_simulated: bool
    answered_at: datetime
    facts: list[Fact] = field(default_factory=list)
    superseded: dict[str, str] = field(default_factory=dict)  # старый факт → новый
    conflicts: list[tuple[Fact, Fact]] = field(default_factory=list)  # (наш расчёт, ответ)
    placements: PlacementsIn | None = None
    room_type_id: str | None = None
    placement_status: str | None = None
    placement_source: dict[str, Any] = field(default_factory=dict)


def _params_key(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


def values_differ(a: Any, b: Any) -> bool:
    """Расходятся ли значения. У словарей сравниваются только общие поля: у расчёта и ответа
    разный набор полей, и это ещё не противоречие."""
    if isinstance(a, dict) and isinstance(b, dict):
        return any(a[k] != b[k] for k in a.keys() & b.keys())
    return a != b


def build_answer(
    question: dict[str, Any], answer: AnswerIn, existing: list[Fact], now: datetime
) -> AnswerResult:
    """Ответ → факты, замены, противоречия, размещения. Ничего не пишет — это делает хранилище."""
    expected = question.get("parsed_fields") or []
    if answer.fields and expected:
        extra = sorted(set(answer.fields) - set(expected))
        if extra:
            raise AnswerError(
                f"поля не из вопроса: {', '.join(extra)}; ждём: {', '.join(expected)}"
            )

    hotel_id, feature_id = question["hotel_id"], question["feature_id"]
    status = Status.simulated_hotelier if answer.is_simulated else Status.hotelier
    room_type_id = answer.room_type_id or (answer.fields or {}).get("room_type_id")
    if room_type_id:
        level, subject = "room_type", room_type_id
    else:
        level, subject = "hotel", f"hotel:{hotel_id}"
    source = {
        "provider": "hotelier_simulator" if answer.is_simulated else "hotelier",
        "question_id": question["question_id"],
        "answered_at": now.isoformat(),
        "respondent": answer.respondent,
        "quote": answer.answer_text,
    }
    result = AnswerResult(
        question_id=question["question_id"],
        hotel_id=hotel_id,
        feature_id=feature_id,
        state="answered_unparsed",
        answer_text=answer.answer_text,
        parsed=answer.fields,
        is_simulated=answer.is_simulated,
        answered_at=now,
    )
    if answer.placements:
        result.placements = answer.placements
        result.room_type_id = room_type_id
        result.placement_status = status.value
        result.placement_source = {k: v for k, v in source.items() if v is not None}
        result.state = "answered"

    if not answer.fields and not answer.dont_know:
        return result  # текст без полей: ждёт разбора агентом

    # ID зависит от вопроса, статуса, содержания и времени: два разных ответа в одну и ту же
    # секунду (например, имитатор и отельер) не должны слиться в один факт.
    content = json.dumps(
        {"fields": answer.fields, "dont_know": answer.dont_know}, sort_keys=True, default=str
    )
    version = f"{ALGORITHM}:{question['question_id']}:{status.value}:{now.isoformat()}:{content}"
    fact = Fact(
        fact_id=make_fact_id(hotel_id, subject, feature_id, answer.params, version),
        hotel_id=hotel_id,
        city_id=question["city_id"],
        subject_level=level,
        subject_id=subject,
        feature_id=feature_id,
        params=answer.params,
        value=None if answer.dont_know else answer.fields,
        status=Status.unknown if answer.dont_know else status,
        unknown_reason="hotelier_does_not_know" if answer.dont_know else None,
        sources=[{k: v for k, v in source.items() if v is not None}],
        recipe_id=f"{feature_id}-HOTELIER",
        algorithm_version=ALGORITHM,
        confidence={
            "level": "low" if answer.is_simulated else "medium",
            "reason": "ответ имитатора — для показа" if answer.is_simulated else "со слов отеля",
        },
        observed_at=now,
        scope_note="Ответ отеля на вопрос; не условие брони.",
        hard_condition_eligible=not answer.dont_know,
    )
    result.facts.append(fact)
    result.state = "answered_unknown" if answer.dont_know else "answered"
    if answer.dont_know:
        return result  # «не знаю» не отменяет того, что известно

    same_place = [
        f
        for f in existing
        if f.subject_id == subject and _params_key(f.params) == _params_key(fact.params)
    ]
    replaceable = SUPERSEDES[status]
    for old in same_place:
        if old.fact_id == fact.fact_id:
            continue
        if old.status in replaceable:
            result.superseded[old.fact_id] = fact.fact_id
        elif old.status in COMPARABLE and values_differ(old.value, fact.value):
            result.conflicts.append((old, fact))
    return result
