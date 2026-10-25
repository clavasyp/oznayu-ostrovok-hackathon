"""Спецификация характеристики — формат core.md §3 (YAML в `specs/`).

Здесь модель черновика: то, что админ получает из текста, правит, гоняет пробный расчёт и
отдаёт на включение. Включение (`status: active`) и папка `specs/` — у лида.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SubjectLevel = Literal[
    "hotel", "building", "facade", "facade_physical_floor", "room_type", "placement", "route",
    "marine_cell",
]  # fmt: skip
ValueType = Literal["number", "integer", "interval", "bool", "enum", "string"]
Op = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"]
RecipeKind = Literal["computed", "observed", "hotelier", "curated"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueField(_Strict):
    type: ValueType
    unit: str | None = None
    values: list[str] | None = None  # для enum


class SearchSpec(_Strict):
    default_field: str
    operators: list[Op]
    synonyms: list[str] = Field(default_factory=list)


class Requires(_Strict):
    layers: list[str] = Field(default_factory=list)
    object_check: str | None = None


class Recipe(_Strict):
    id: str
    kind: RecipeKind
    calculator: str | None = None  # «env.landcover_share@1»
    params: dict[str, Any] = Field(default_factory=dict)
    targets: str | None = None
    requires: Requires | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    publish: dict[str, Any] | None = None
    question: str | None = None  # для kind = hotelier
    parse_to: list[str] = Field(default_factory=list)


class FeatureSpec(_Strict):
    id: str
    version: int = Field(default=1, ge=1)
    title: str
    family: str
    subject_level: SubjectLevel
    status: Literal["draft", "active", "retired"] = "draft"
    value: dict[str, ValueField]
    search: SearchSpec
    recipes: list[Recipe]
    related: list[str] = Field(default_factory=list)
    ttl_days: int = Field(default=365, ge=1, le=3650)
    source_text: str | None = None  # текст админа, из которого сделан черновик


def to_yaml(spec: FeatureSpec) -> str:
    data = spec.model_dump(mode="json", exclude_none=True)
    for recipe in data["recipes"]:  # пустые params / parse_to в YAML — только шум
        for key in [k for k, v in recipe.items() if v in ([], {})]:
            del recipe[key]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def from_yaml(text: str) -> FeatureSpec:
    """YAML → спецификация. Неверная структура — `pydantic.ValidationError` / `yaml.YAMLError`."""
    return FeatureSpec.model_validate(yaml.safe_load(text))
