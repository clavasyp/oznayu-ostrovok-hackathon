"""Разобранный ответ отельера → факт с паспортом и сверка с заявлениями Островка.

Ответ имитатора — статус `simulated_hotelier` (в реальное покрытие не входит), настоящего
отельера — `hotelier`. Ответ «для части номеров» обязательное условие не закрывает (D17).
Расхождение с карточкой Островка того же смысла — противоречие `detected`, а не перезапись.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents.catalog import Feature
from agents.hotelier.parse_answer import ParsedAnswer
from agents.hotelier.schemas import CLAIM_FIELDS, CLAIM_TAGS, label
from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id

ALGORITHM = "hotelier_answer@1"
LEVELS = {"placement": "room_type", "facade": "room_type"}


def subject_for(
    hotel_id: str, feature: Feature, room_type_id: str | None = None
) -> tuple[str, str]:
    level = LEVELS.get(feature.level, feature.level)
    if level == "hotel":
        return level, hotel_id
    if room_type_id:
        return level, room_type_id
    return level, f"{hotel_id}:*"  # все категории / корпуса отеля — квантор в факте


def answer_fact(
    *,
    hotel_id: str,
    city_id: str,
    feature: Feature,
    parsed: ParsedAnswer,
    question_id: str,
    is_simulated: bool,
    room_type_id: str | None = None,
    at: datetime | None = None,
) -> Fact | None:
    if parsed.answer_state not in ("answered", "varies") or not parsed.accepted:
        return None
    at = at or datetime.now(UTC)
    level, subject_id = subject_for(hotel_id, feature, room_type_id)
    algorithm = ALGORITHM + (":simulated" if is_simulated else "")
    quantifier = parsed.quantifier if parsed.quantifier in ("all", "some", "none") else None
    if parsed.answer_state == "varies" and quantifier is None:
        quantifier = "some"
    who = "имитатор отельера" if is_simulated else "отельер"
    return Fact(
        fact_id=make_fact_id(hotel_id, subject_id, feature.id, {}, algorithm),
        hotel_id=hotel_id,
        city_id=city_id,
        subject_level=level,
        subject_id=subject_id,
        feature_id=feature.id,
        params={},
        value={name: a.value for name, a in parsed.accepted.items()},
        status=Status.simulated_hotelier if is_simulated else Status.hotelier,
        sources=[Source(provider="hotelier_simulator" if is_simulated else "hotelier")],
        recipe_id="ask_hotelier",
        algorithm_version=algorithm,
        confidence=Confidence(level="medium", reason=f"{who}: ответ свободным текстом с цитатой"),
        observed_at=at,
        calculated_at=at,
        scope_note=parsed.conditions or None,
        recipe_attempts=[RecipeAttempt(recipe_id="ask_hotelier", state="accepted", data_kind="В")],
        quantifier=quantifier,
        # «Для части номеров» не гарантирует размещение — обязательное не закрывает (D17).
        hard_condition_eligible=quantifier != "some",
        is_simulated=is_simulated,
        question_id=question_id,
        quotes={name: a.quote for name, a in parsed.accepted.items()},
        parsed_by=parsed.parsed_by,
    )


def conflicts_with_ostrovok(
    fact: Fact, claims: list[dict[str, Any]], hotel_name: str | None = None
) -> list[dict[str, Any]]:
    """Заявления Островка того же смысла, которые расходятся с ответом отельера."""
    out = []
    value = fact.value if isinstance(fact.value, dict) else {}
    for claim in claims:
        atom = claim.get("atom_id")
        name = CLAIM_FIELDS.get((fact.feature_id, atom))
        claimed = claim.get("value")
        if name is None and atom == "published_room_equipment_tag":
            name, claimed = CLAIM_TAGS.get((fact.feature_id, claim.get("value"))), True
        if name is None or name not in value or claimed is None or claimed == value[name]:
            continue
        out.append(
            {
                "city_id": fact.city_id,
                "hotel_id": fact.hotel_id,
                "name": hotel_name,
                "feature_id": fact.feature_id,
                "field": name,
                "ota_value": claim.get("quote"),
                "other_value": f"{label(name)}: {'да' if value[name] else 'нет'}",
                "classification": "hotelier_vs_ostrovok_card",
                "resolution": "Карточка Островка и ответ отельера расходятся. Пока противоречие "
                "не разрешено (документ или фото), обязательное условие — «Нужно уточнить».",
                "ota_url": claim.get("source_url"),
                "ota_pointer": claim.get("json_pointer"),
                "state": "detected",
                "fact_id": fact.fact_id,
                "is_simulated": bool(getattr(fact, "is_simulated", False)),
            }
        )
    return out
