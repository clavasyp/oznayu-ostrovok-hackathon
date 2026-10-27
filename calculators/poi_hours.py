"""Проверка регулярных часов в заданный местный момент, как в компактном v9.

Не ищет next_change: эта необязательная операция зависала на календарях Москвы.
Часы кухни отделены от часов заведения. Сеть не используется.
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import opening_hours


@lru_cache(maxsize=32768)
def _state(expression: str, local_time: str, timezone: str, country: str) -> dict:
    try:
        date = datetime.fromisoformat(local_time)
        date = (
            date.replace(tzinfo=ZoneInfo(timezone))
            if date.tzinfo is None
            else date.astimezone(ZoneInfo(timezone))
        )
        hours = opening_hours.OpeningHours(expression, country=country, timezone=ZoneInfo(timezone))
        state = "open" if hours.is_open(date) else "closed" if hours.is_closed(date) else "unknown"
        return {
            "state": state,
            "expression": expression,
            "evaluated_local": date.isoformat(),
            "has_public_holiday_expression": bool(re.search(r"\b(PH|SH)\b", expression)),
            "next_change": None,
            "reason": (
                "Объявленные регулярные часы; фактическая работа и будущие исключения не проверены."
            ),
        }
    except (
        ValueError,
        TypeError,
        opening_hours.ParserError,
        opening_hours.UnknownCountryError,
        opening_hours.InvalidCoordinatesError,
    ) as exc:
        return {
            "state": "unknown",
            "expression": expression,
            "reason": f"opening_hours_parser_failed:{type(exc).__name__}",
        }


def opening_state(
    tags: dict, local_time: str, *, timezone: str, country: str = "RU", kitchen: bool = False
) -> dict:
    keys = (
        ("opening_hours:kitchen", "kitchen:opening_hours", "food_service_hours")
        if kitchen
        else ("opening_hours",)
    )
    expression = next((tags[k] for k in keys if tags.get(k)), None)
    if not expression:
        return {
            "state": "unknown",
            "expression": None,
            "reason": "no_kitchen_service_hours" if kitchen else "no_opening_hours",
        }
    return dict(_state(expression, local_time, timezone, country))
