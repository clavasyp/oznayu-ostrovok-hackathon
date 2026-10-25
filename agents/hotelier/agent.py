"""Агент-отельер: свободный ответ → тело `POST /api/hotelier/{id}/answers`.

Контракт — `core/hotelier.py` и `docs/design/contracts.md`, «Ответ отельера → факт → пересчёт».

Что становится фактом, решает `core/hotelier.py` (статус, замена заявлений, противоречия). Агент
только переводит текст в поля вопроса и честно говорит, чего не понял:
- поля — ровно из `parsed_fields` вопроса, с цитатами (иначе 422);
- «не знаю» → `dont_know`;
- ответ «для части номеров», «зависит от номера или даты», противоречивый или без полей —
  только текст (`answered_unparsed`) и уточняющий вопрос: такой ответ не должен закрыть
  обязательное условие гостя.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.hotelier.parse_answer import parse_answer
from agents.llm import Gateway


@dataclass
class AgentAnswer:
    body: dict[str, Any] = field(default_factory=dict)  # fields | dont_know — в тело ответа
    report: dict[str, Any] = field(default_factory=dict)  # что понял агент, для экрана


def answer_from_text(
    question: dict[str, Any], answer_text: str, *, gateway: Gateway | None = None
) -> AgentAnswer:
    parsed = parse_answer(
        question["feature_id"], answer_text, question.get("question") or "", gateway=gateway
    )
    expected = set(question.get("parsed_fields") or [])
    fields = {k: v for k, v in parsed.fields.items() if not expected or k in expected}
    body: dict[str, Any] = {}
    if parsed.answer_state == "unknown":
        body = {"dont_know": True}
    elif parsed.answer_state == "answered" and parsed.quantifier != "some" and fields:
        # «Зависит от номера или даты» (varies) — то же, что «для части номеров»: не факт на отель.
        body = {"fields": fields}
    report = {
        "parsed_by": parsed.parsed_by,
        "answer_state": parsed.answer_state,
        "quantifier": parsed.quantifier,
        "fields": {
            name: {"value": a.value, "quote": a.quote, "span": [a.start, a.end]}
            for name, a in parsed.accepted.items()
            if name in fields
        },
        "rejected": parsed.rejected,
        "followup": parsed.followup,
        "conditions": parsed.conditions or None,
        "notes": parsed.notes,
    }
    return AgentAnswer(body=body, report=report)
