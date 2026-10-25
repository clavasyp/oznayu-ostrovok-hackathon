"""Имитатор отельера (core §7.2, v4b п.6): отвечает свободным текстом по закрытому профилю отеля.

- Профиль — эталонная правда для показа (`profiles/<hotel_id>.json`), а не факты: всё, что
  из него выходит, помечено `is_simulated` и становится `simulated_hotelier`.
- Генератор не видит запрос гостя — только вопрос анкеты.
- Ответы программные и детерминированные (фиксированный seed): работают без ключа LLM.
  Стиль профиля задаёт «живость»: `plain` — все поля, `vague` — только главное («да, лифт есть»).
"""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents.catalog import load_catalog
from agents.hotelier.schemas import label

PROFILES = Path(__file__).parent / "profiles"
SIMULATED_NOTE = "Ответ имитатора отельера — для показа, не настоящий отельер"

UNIT_RU = {
    "mm": "мм", "kg": "кг", "RUB": "₽", "Mbit/s": "Мбит/с", "ms": "мс", "m": "м", "m2": "м²",
    "years": "лет", "kW": "кВт", "%": "%", "dB(A)": "дБ(A)", "h": "ч",
}  # fmt: skip
YES = {
    "has_ac": "Да, кондиционер есть во всех номерах.",
    "individual_control": "Да, гость может сам включать и регулировать кондиционер с пульта.",
    "full_blackout": "Да, шторы blackout полностью затемняют комнату.",
    "edge_gaps": "Да, по краям штор остаются небольшие просветы.",
    "own_parking": "Да, у отеля своя парковка.",
    "reservation": "Да, место нужно бронировать заранее.",
    "has_charger": "Да, на территории есть зарядная станция.",
    "guest_access": "Да, гости могут ей пользоваться.",
    "checkin_possible": "Да, заселение в это время возможно.",
    "front_desk_24h": "Да, стойка регистрации работает круглосуточно.",
    "self_checkin": "Да, есть самозаселение по коду.",
    "prior_arrangement": "Да, о позднем приезде нужно предупредить заранее.",
    "breakfast_box": "Да, можно взять завтрак с собой.",
    "cot_available": "Да, детскую кроватку поставим.",
    "connecting_rooms": "Да, есть смежные номера.",
    "kids_pool": "Да, детский бассейн есть.",
    "supervised": "Да, за детьми присматривает инструктор.",
    "pets_allowed": "Да, с животными можно.",
    "prior_approval": "Да, питомца нужно заранее согласовать.",
    "step_free": "Да, вход без ступеней, есть пандус.",
    "has_lift": "Да, лифт есть.",
    "step_free_shower": "Да, душ без бортика.",
    "has_desk": "Да, в номере есть рабочий стол.",
    "socket_near_desk": "Да, розетка рядом со столом.",
    "private_balcony": "Да, в номерах этой категории есть балкон.",
    "private_terrace": "Да, есть терраса.",
    "private_kitchenette": "Да, в номере есть кухонная зона.",
    "cooking_allowed": "Да, готовить можно.",
    "in_room_kettle": "Да, чайник есть в номере.",
    "has_safe": "Да, сейф есть.",
    "assignment_guarantee": "Да, это можно гарантировать при бронировании.",
    "guaranteed_pair": "Да, пару номеров можно гарантировать.",
    "shared_wall_with_lift": "Да, у части номеров есть общая стена с лифтовой шахтой.",
}
NO = {
    "has_ac": "Нет, кондиционеров в номерах нет.",
    "pets_allowed": "Нет, с животными, к сожалению, нельзя.",
    "front_desk_24h": "Нет, стойка регистрации работает не круглосуточно.",
    "full_blackout": "Нет, полного затемнения нет.",
    "step_free": "Нет, на входе есть ступени, пандуса нет.",
    "has_lift": "Нет, лифта нет.",
    "reservation": "Нет, бронировать место заранее не нужно.",
    "breakfast_box": "Нет, завтрак с собой не собираем.",
    "has_desk": "Нет, рабочего стола в номере нет.",
    "step_free_shower": "Нет, у душа есть бортик.",
}
VALUES_RU = {
    "dog": "собаками", "cat": "кошками", "per_night": "за сутки", "per_stay": "за всё проживание",
    "full": "полностью", "partial": "частично", "none": "никак",
    "closed": "закрытых", "open": "открытых",
    "type2": "Type 2", "double": "двуспальная", "twin": "раздельные",
    "double_or_twin": "двуспальная или раздельные",
}  # fmt: skip
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
    if isinstance(value, bool):
        return (
            YES.get(field, f"Да, {name} — есть.") if value else NO.get(field, f"Нет, {name} — нет.")
        )
    if field == "species":
        return "Можно с " + " и ".join(VALUES_RU.get(v, v) for v in value) + "."
    if field == "fee_basis":
        return f"Доплата берётся {VALUES_RU.get(value, value)}."
    if field == "windows_state":
        return f"Замер при {VALUES_RU.get(value, value)} окнах."
    if isinstance(value, (int, float)):
        if unit == "RUB" and value == 0:
            return f"{title}: бесплатно."
        return f"{title}: {value:g} {UNIT_RU.get(unit or '', unit or '')}".rstrip() + "."
    if field.endswith("_start") or field == "breakfast_start":
        return f"{title} — с {value}."
    return f"{title}: {VALUES_RU.get(value, value)}."


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
        if entry.get("quantifier") == "some":
            parts.append("Это не во всех номерах категории.")
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
