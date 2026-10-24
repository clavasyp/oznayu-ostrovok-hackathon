"""Контракт 1: факт с паспортом — формат обмена между калькуляторами, коннекторами, базой и API.

Имена полей — как в фактах расчёта v9
(`research_notes/Расчёт характеристик/integrated/*_facts.jsonl`),
чтобы готовые результаты грузились без адаптера. Лишние поля v9 (диагностика) сохраняются как есть.
Правила: D5 (да / нет / неизвестно), D8 (LLM не источник фактов), D18 (оценки не в обязательных
условиях), D26 (данные Островка), D33 (`curated` — только после проверки человеком).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Status(StrEnum):
    observed = "observed"  # значение первоисточника (OSM, реестр)
    computed = "computed"  # посчитано нашим калькулятором
    estimate = "estimate"  # оценка ниже порога публикации (D18): только в мягких условиях
    ostrovok_content = "ostrovok_content"  # опубликовано на странице Островка (D26)
    curated = "curated"  # обработано командой с ИИ и проверено человеком (D33)
    hotelier = "hotelier"  # ответ отельера
    simulated_hotelier = (
        "simulated_hotelier"  # ответ имитатора: показ, в реальное покрытие не входит
    )
    unknown = "unknown"  # неизвестно, причина в unknown_reason
    not_applicable = "not_applicable"  # признак неприменим (например, моря нет)


# Статусы, которые могут закрыть обязательное условие (если hard_condition_eligible не False).
HARD_ELIGIBLE = frozenset(
    {
        Status.observed,
        Status.computed,
        Status.ostrovok_content,
        Status.curated,
        Status.hotelier,
        Status.simulated_hotelier,
    }
)
# Статусы без значения.
VALUELESS = frozenset({Status.unknown, Status.not_applicable})


class SubjectLevel(StrEnum):
    hotel = "hotel"
    building = "building"
    building_part = "building_part"
    facade = "facade"
    physical_floor = "physical_floor"
    room_type = "room_type"
    placement = "placement"
    route = "route"


class Confidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    level: Literal["high", "medium", "low", "unknown"] | None = (
        None  # v9: unknown у неизвестных фактов
    )
    reason: str | None = None


class Source(BaseModel):
    """Происхождение: провайдер, версия набора, хеш, лицензия, ссылка.

    Дополнительные поля — как у источника.
    """

    model_config = ConfigDict(extra="allow")

    provider: str
    url: str | None = None
    license: str | None = None
    dataset_version: str | None = None
    dataset_sha256: str | None = None


class RecipeAttempt(BaseModel):
    """Попытка рецепта: что пробовали и почему принято или отклонено (D10, D15)."""

    model_config = ConfigDict(extra="allow")

    recipe_id: str
    state: str  # accepted | rejected | skipped | failed
    reason: str | None = None
    data_kind: str | None = None  # А — готовые, Б — вычисляемые, В — отельер


class Fact(BaseModel):
    model_config = ConfigDict(extra="allow", use_enum_values=False)

    fact_id: str
    hotel_id: str
    city_id: str
    subject_level: SubjectLevel
    subject_id: str
    feature_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    value: Any = None
    unit: str | None = None
    status: Status
    unknown_reason: str | None = None
    sources: list[Source] = Field(default_factory=list)
    recipe_id: str | None = None
    algorithm_version: str | None = None
    confidence: Confidence | None = None
    observed_at: datetime | None = None
    calculated_at: datetime | None = None
    valid_to: datetime | None = None
    scope_note: str | None = None
    recipe_attempts: list[RecipeAttempt] = Field(default_factory=list)
    depends_on: list[Any] = Field(default_factory=list)
    hard_condition_eligible: bool | None = None
    refresh_after: Any = None
    quantifier: Literal["all", "some", "none"] | None = None  # для уровня категории

    @model_validator(mode="after")
    def _status_rules(self) -> Fact:
        if self.status == Status.unknown and not self.unknown_reason:
            raise ValueError("status=unknown требует unknown_reason")
        if self.status not in VALUELESS and self.value is None:
            raise ValueError(f"status={self.status} требует value")
        return self

    @property
    def usable_in_hard(self) -> bool:
        """Можно ли этим фактом закрыть обязательное условие (D18, D26, D33)."""
        return self.status in HARD_ELIGIBLE and self.hard_condition_eligible is not False


def make_fact_id(
    hotel_id: str, subject_id: str, feature_id: str, params: dict[str, Any], algorithm_version: str
) -> str:
    """Детерминированный ID факта: 24 hex, как в v9. Тот же вход — тот же ID."""
    key = json.dumps(
        [hotel_id, subject_id, feature_id, params, algorithm_version],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
