"""Имитатор отельера (core §7.2, v4b п.6): отвечает свободным текстом по закрытому профилю отеля.

- Профиль — эталонная правда для показа (`profiles/<hotel_id>.json`), а не факты: ответ уходит
  в `POST /api/hotelier/{id}/answers` с `is_simulated = true` и становится `simulated_hotelier`.
- Генератор не видит запрос гостя — только вопрос анкеты.
- Поля профиля — ровно `parsed_fields` вопроса. Ответы программные и детерминированные
  (фиксированный seed): работают без ключа LLM. Стиль `vague` — только главное («да, лифт есть»).
"""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents.catalog import load_catalog
from agents.hotelier.schemas import PRESENCE, UNIT_RU, label

PROFILES = Path(__file__).parent / "profiles"
SIMULATED_NOTE = "Ответ имитатора отельера — для показа, не настоящий отельер"

YES = {
    "individual_control": "Да, гость может сам включать и регулировать кондиционер с пульта.",
    "edge_gaps": "Да, по краям штор остаются небольшие просветы.",
    "shared_wall_with_lift": "Да, у части номеров есть общая стена с лифтовой шахтой.",
    "assignment_guarantee": "Да, это можно гарантировать при бронировании.",
    "reservation": "Да, место нужно бронировать заранее.",
    "external_access": "Да, зарядкой могут пользоваться все, не только гости.",
    "self_checkin": "Да, есть самозаселение по коду.",
    "prior_arrangement": "Да, о позднем приезде нужно предупредить заранее.",
    "breakfast_box": "Да, можно взять завтрак с собой.",
    "reservation_guarantee": "Да, кроватку гарантируем при бронировании.",
    "internal_connecting_door": "Да, есть смежные номера с внутренней дверью.",
    "guaranteed_pair": "Да, пару номеров можно гарантировать.",
    "prior_approval": "Да, питомца нужно заранее согласовать.",
    "split_possible": "Да, кровати можно раздвинуть.",
    "private_balcony": "Да, в номерах этой категории есть балкон.",
    "private_terrace": "Да, есть терраса.",
    "private_kitchenette": "Да, в номере есть кухонная зона.",
    "cooking_allowed": "Да, готовить можно.",
    "in_room_kettle": "Да, чайник есть в номере.",
    "on_request": "Да, по запросу.",
    "guarantee": "Да, гарантируем.",
}
NO = {
    "reservation": "Нет, бронировать место заранее не нужно.",
    "self_checkin": "Нет, самозаселения нет.",
    "breakfast_box": "Нет, завтрак с собой не собираем.",
    "private_balcony": "Нет, балкона в номерах этой категории нет.",
}
PRESENT = {
    "hvac_type": "Да, кондиционер есть во всех номерах.",
    "connector": "Да, на территории есть зарядная станция.",
    "cot_dimensions": "Да, детскую кроватку поставим.",
    "pool_id": "Да, детский бассейн есть.",
    "lift_id": "Да, лифт есть.",
    "access": "Да, сейф есть.",
}
ABSENT = {
    "hvac_type": "Нет, кондиционеров в номерах нет.",
    "connector": "Нет, зарядки для электромобилей нет.",
    "cot_dimensions": "Нет, детских кроваток нет.",
    "pool_id": "Нет, детского бассейна нет.",
    "lift_id": "Нет, лифта нет.",
    "access": "Нет, сейфа нет.",
    "parking_owner": "Нет, своей парковки нет.",
}
ENUM_SENTENCES = {
    ("hvac_type", "split"): "Да, в номерах сплит-системы.",
    ("hvac_type", "central"): "Да, кондиционирование центральное.",
    ("connector", "type2"): "Да, есть зарядная станция с разъёмом Type 2.",
    ("parking_owner", "own"): "Да, у отеля своя парковка.",
    ("parking_owner", "third_party"): "Парковка сторонняя, городская.",
    ("opaque_coverage", "full"): "Шторы закрывают окна полностью.",
    ("opaque_coverage", "partial"): "Шторы закрывают окна частично.",
    ("species", "dog"): "Можно с собаками.",
    ("species", "cat"): "Можно с кошками.",
    ("species", "dog_cat"): "Можно с собаками и кошками.",
    ("species", "any"): "Можно с любыми животными.",
    ("species", "none"): "Нет, с животными, к сожалению, нельзя.",
    ("fee_basis", "per_night"): "Доплата берётся за сутки.",
    ("fee_basis", "per_stay"): "Доплата берётся за всё проживание.",
    ("windows_state", "closed"): "Замер при закрытых окнах.",
    ("windows_state", "open"): "Замер при открытых окнах.",
    ("bed_configuration", "double"): "Кровать двуспальная.",
    ("bed_configuration", "twin"): "Кровати раздельные.",
    ("bed_configuration", "double_or_twin"): "Кровати: двуспальная или раздельные.",
}
SIDES_RU = {
    "N": "север", "NE": "северо-восток", "E": "восток", "SE": "юго-восток", "S": "юг",
    "SW": "юго-запад", "W": "запад", "NW": "северо-запад",
}  # fmt: skip
OPENERS = ["", "Добрый день! ", "Здравствуйте! "]
CLOSERS = ["", " Будем рады вас видеть.", " Если будут вопросы — пишите."]


@lru_cache(maxsize=32)
def load_profile(hotel_id: str) -> dict[str, Any] | None:
    path = PROFILES / f"{hotel_id}.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None


def profiled_hotels() -> list[str]:
    return sorted(p.stem for p in PROFILES.glob("*.json"))


def _sentence(field: str, value: Any, unit: str | None) -> str:
    name = label(field).split(",")[0]
    title = name[:1].upper() + name[1:]
    if (field, value) in ENUM_SENTENCES:
        return ENUM_SENTENCES[(field, value)]
    if field in PRESENCE and value == PRESENCE[field][1]:
        return ABSENT.get(field, f"Нет, {name} — нет.")
    if field in PRESENCE:
        return PRESENT.get(field, f"Да, {name} — есть.")
    if field == "front_desk_hours":
        if value == "24/7":
            return "Да, стойка регистрации работает круглосуточно."
        start, _, end = str(value).partition("-")
        return f"Стойка регистрации работает с {start} до {end}."
    if isinstance(value, bool):
        return (
            YES.get(field, f"Да, {name} — есть.") if value else NO.get(field, f"Нет, {name} — нет.")
        )
    if isinstance(value, (int, float)):
        if unit == "RUB" and value == 0:
            return f"{title}: бесплатно."
        return f"{title}: {value:g} {UNIT_RU.get(unit or '', unit or '')}".rstrip() + "."
    if field == "breakfast_start":
        return f"{title} — с {value}."
    return f"{title}: {value}."


def simulate_answer(hotel_id: str, feature_id: str, *, seed: int | None = None) -> dict[str, Any]:
    """Свободный ответ имитатора на вопрос анкеты. Всегда `is_simulated = True`."""
    profile = load_profile(hotel_id) or {}
    entry = (profile.get("answers") or {}).get(feature_id)
    rng = random.Random(
        f"{seed if seed is not None else profile.get('seed', 0)}:{hotel_id}:{feature_id}"
    )
    if entry is None:
        text = "К сожалению, точной информации нет — уточним и вернёмся."
    elif "say" in entry:
        text = entry["say"]
    else:
        feature = load_catalog().get(feature_id)
        fields: dict[str, Any] = entry.get("fields", {})
        style = entry.get("style") or profile.get("style", "plain")
        order = [feature.default_field] + [f for f in fields if f != feature.default_field]
        if style == "vague":
            order = order[:1]
        parts = [
            _sentence(f, fields[f], feature.fields[f].unit)
            for f in order
            if f in fields and f in feature.fields
        ]
        if entry.get("conditions"):
            parts.append(entry["conditions"])
        text = " ".join(parts)
    text = rng.choice(OPENERS) + text + rng.choice(CLOSERS)
    return {
        "hotel_id": hotel_id,
        "feature_id": feature_id,
        "answer_text": text,
        "is_simulated": True,
        "note": SIMULATED_NOTE,
    }


def simulate_placement(hotel_id: str, room_type_id: str) -> dict[str, Any]:
    """Ответ на адресный вопрос: стороны и этажи окон категории — из профиля."""
    profile = load_profile(hotel_id) or {}
    cat = next(
        (c for c in profile.get("categories", []) if c["room_type_id"] == room_type_id), None
    )
    if cat is None:
        text = "Точно сказать не можем — зависит от конкретного номера."
    else:
        parts = []
        for p in cat["placements"]:
            side = "во двор" if p["side"] == "courtyard" else f"на {SIDES_RU[p['side']]}"
            lo, hi = p["floors"]
            parts.append(f"окна выходят {side}, с {lo} по {hi} этаж ({p['building']})")
        text = f"В категории «{cat['name']}» " + "; ".join(parts) + "."
        if cat.get("completeness") == "complete":
            text += " Других вариантов в этой категории нет."
        text += {
            "all_guaranteed": " Это гарантируем при бронировании.",
            "on_request": " Конкретный вариант — по запросу, гарантировать заранее не можем.",
        }.get(cat.get("guarantee", ""), "")
    return {
        "hotel_id": hotel_id,
        "feature_id": "category_placement",
        "room_type_id": room_type_id,
        "answer_text": text,
        "is_simulated": True,
        "note": SIMULATED_NOTE,
    }
