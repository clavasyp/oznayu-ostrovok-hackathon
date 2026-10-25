"""Вопросы отельеру с предзаполнением (core §7.2): 26 вопросов v9 + адресные вопросы расчёта.

Предзаполнение — заявления со страницы Островка (v9 `prefill.jsonl`, для отелей показа —
`app/fixtures/questions.json`): «на Островке указано так — верно?». Это заявление, а не
проверка: вопрос задаётся всё равно, ответ отельера сверяется с ним.
"""

from __future__ import annotations

import csv
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents.catalog import load_catalog
from agents.hotelier.schemas import CLAIM_FIELDS, label

# Копия `research_notes/Расчёт характеристик/hotelier/questions_26.csv` (v9): research_notes
# в образ приложения не входит, а анкета нужна и при запуске у жюри.
QUESTIONS_CSV = Path(__file__).parent / "questions_26.csv"


@lru_cache(maxsize=1)
def question_texts() -> dict[str, dict[str, str]]:
    with QUESTIONS_CSV.open(encoding="utf-8") as f:
        return {r["feature_id"]: r for r in csv.DictReader(f)}


def question_id(hotel_id: str, feature_id: str, subject: str = "") -> str:
    return "q_" + hashlib.sha256(f"{hotel_id}|{feature_id}|{subject}".encode()).hexdigest()[:16]


def prefill_text(feature_id: str, claims: list[dict[str, Any]]) -> str | None:
    """«На Островке указано: …» — по заявлениям уровня отеля, затем категорий, без повторов."""
    quotes: list[str] = []
    for c in sorted(claims, key=lambda c: c.get("subject_type") != "hotel"):
        quote = " ".join(str(c.get("quote") or "").split())
        if not quote or quote.startswith("{") or quote in quotes:
            continue
        quotes.append(quote)
    if not quotes:
        return None
    date = next((str(c.get("fetched_at", ""))[:10] for c in claims if c.get("fetched_at")), "")
    shown = "», «".join(quotes[:3])
    tail = f" (страница от {date})" if date else ""
    return f"На странице отеля на Островке указано: «{shown}»{tail}. Это верно?"


def build_question(hotel_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Вопрос анкеты: текст v9, предзаполнение, поля ответа, что из предзаполнения сверяется."""
    feature_id = raw["feature_id"]
    feature = load_catalog().get(feature_id)
    spec = question_texts().get(feature_id, {})
    claims = raw.get("claims") or []
    checked = sorted({f for c in claims if (f := CLAIM_FIELDS.get((feature_id, c.get("atom_id"))))})
    return {
        "question_id": question_id(hotel_id, feature_id),
        "hotel_id": hotel_id,
        "feature_id": feature_id,
        "title": feature.short if feature else spec.get("name"),
        "scope": spec.get("scope"),
        "text": raw.get("question") or spec.get("free_text_question"),
        "prefill": prefill_text(feature_id, claims),
        "prefill_claims": [
            {k: c.get(k) for k in ("quote", "source_url", "fetched_at", "subject_type", "status")}
            for c in claims
        ],
        "prefill_checks_fields": checked,
        "fields": [
            {
                "name": s.name,
                "type": s.type,
                "unit": s.unit,
                "values": list(s.values),
                "label": label(s.name),
            }  # fmt: skip
            for s in (feature.fields.values() if feature else [])
        ],
        "answer_status": raw.get("answer_status", "not_asked"),
        "is_simulated": False,
    }


def questionnaire(hotel_id: str, raw_questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Анкета отеля: сначала вопросы с предзаполнением (подтвердить быстрее), порядок стабилен."""
    qs = [build_question(hotel_id, raw) for raw in raw_questions]
    return sorted(qs, key=lambda q: q["prefill"] is None)


def placement_question(hotel_id: str, room_type: dict[str, Any]) -> dict[str, Any]:
    """Адресный вопрос из расчёта вида и солнца: сторона и этажи окон категории (D17)."""
    rt_id = room_type["room_type_id"]
    name = room_type.get("name") or rt_id
    return {
        "question_id": question_id(hotel_id, "category_placement", rt_id),
        "hotel_id": hotel_id,
        "feature_id": "category_placement",
        "room_type_id": rt_id,
        "title": f"Окна категории «{name}»",
        "text": (
            f"Категория «{name}» ({rt_id}): на какую сторону выходят окна и с каких этажей? "
            "Если по-разному — перечислите варианты «корпус, этажи, сторона». "
            "Можно ли гарантировать конкретный вариант при бронировании?"
        ),
        "prefill": None,
        "is_simulated": False,
    }


ENTRANCE_QUESTION = (
    "Где реальный вход для гостей: с какой улицы и стороны здания? Есть ли ступени или пандус?"
)
