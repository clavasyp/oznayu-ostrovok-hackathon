"""Вопросы отельеру: текст предзаполнения и поля ответа (core §7.2) + адресные вопросы расчёта.

Сами вопросы и их состояние ведёт хранилище (`core.store.Store.questions`). Агент добавляет:
- **предзаполнение** — «на Островке указано так — это верно?» по заявлениям страницы (v9
  `prefill`). Это заявление, а не проверка: вопрос задаётся всё равно;
- **поля ответа** — типы, единицы и подписи для формы кабинета и для разбора ответа;
- **адресные вопросы** «стороны и этажи окон категории» — для вида и солнца (D17).
"""

from __future__ import annotations

import csv
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents.catalog import load_catalog
from agents.hotelier.placement import FEATURE_ID, choice_label, choices_text
from agents.hotelier.schemas import PRESENCE, label

# Копия `research_notes/Расчёт характеристик/hotelier/questions_26.csv` (v9): research_notes
# в образ приложения не входит, а тексты вопросов нужны и при запуске у жюри.
QUESTIONS_CSV = Path(__file__).parent / "questions_26.csv"


@lru_cache(maxsize=1)
def question_texts() -> dict[str, dict[str, str]]:
    with QUESTIONS_CSV.open(encoding="utf-8") as f:
        return {r["feature_id"]: r for r in csv.DictReader(f)}


# Служебные метки страницы Островка: «pets-allowed», «PRIOR_AGREEMENT_CHECK_IN», JSON. Отельеру
# показываем только то, что на странице написано словами.
_SERVICE_TAG = re.compile(r"^(?:[a-z0-9]+(?:[-_][a-z0-9]+)*|[A-Z0-9]+(?:_[A-Z0-9]+)*)$")


def prefill_text(claims: list[dict[str, Any]]) -> str | None:
    """«На Островке указано: …» — по заявлениям уровня отеля, затем категорий, без повторов."""
    quotes: list[str] = []
    for c in sorted(claims, key=lambda c: c.get("subject_type") != "hotel"):
        quote = " ".join(str(c.get("quote") or "").split()).strip('"«»')
        if not quote or quote.startswith(("{", "[")) or _SERVICE_TAG.match(quote):
            continue
        if quote not in quotes:
            quotes.append(quote)
    if not quotes:
        return None
    date = next((str(c.get("fetched_at", ""))[:10] for c in claims if c.get("fetched_at")), "")
    shown = "», «".join(quotes[:3])
    tail = f" (страница от {date})" if date else ""
    return f"На странице отеля на Островке указано: «{shown}»{tail}. Это верно?"


def field_specs(feature_id: str) -> list[dict[str, Any]]:
    feature = load_catalog().get(feature_id)
    if feature is None:
        return []
    return [
        {
            "name": s.name,
            "type": s.type,
            "unit": s.unit,
            "values": list(s.values),
            "label": label(s.name),
            "absent_value": PRESENCE[s.name][1] if s.name in PRESENCE else None,
        }
        for s in feature.fields.values()
    ]


def enrich_question(q: dict[str, Any]) -> dict[str, Any]:
    """Вопрос из хранилища + предзаполнение и поля (новые ключи; старые не меняются)."""
    feature = load_catalog().get(q["feature_id"])
    return q | {
        "title": feature.short
        if feature
        else question_texts().get(q["feature_id"], {}).get("name"),
        "prefill": prefill_text(q.get("claims") or []),
        "fields": field_specs(q["feature_id"]),
    }


def placement_question_id(hotel_id: str, room_type_id: str) -> str:
    return "qp_" + hashlib.sha256(f"{hotel_id}|{room_type_id}".encode()).hexdigest()[:16]


def placement_question(
    hotel_id: str, room_type: dict[str, Any], choices: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Адресный вопрос из расчёта вида и солнца: сторона и этажи окон категории (D17).

    `choices` — корпуса отеля по карте (`placement_building_choices` геометрии). Если их
    несколько, отельер выбирает корпус по адресу и этажности и пишет, как его называет: название
    из OSM — не название отеля, а этажность OSM не доказывает этажи категории.
    """
    rt_id = room_type["room_type_id"]
    name = room_type.get("name") or rt_id
    text = (
        f"Категория «{name}» ({rt_id}): на какую сторону выходят окна и с каких этажей? "
        "Если по-разному — перечислите варианты «корпус, этажи, сторона». "
        "Можно ли гарантировать конкретный вариант при бронировании?"
    )
    if choices and len(choices) > 1:
        text += (
            "\n\nКорпуса отеля по карте — отметьте корпус в списке и напишите, как вы его "
            "называете:\n" + choices_text(choices)
        )
    return {
        "question_id": placement_question_id(hotel_id, rt_id),
        "hotel_id": hotel_id,
        "feature_id": FEATURE_ID,
        "room_type_id": rt_id,
        "title": f"Окна категории «{name}»",
        "question": text,
        "building_choices": [
            {"building_id": c["building_id"], "label": choice_label(c)} for c in choices or []
        ],
    }


ENTRANCE_QUESTION = (
    "Где реальный вход для гостей: с какой улицы и стороны здания? Есть ли ступени или пандус?"
)
