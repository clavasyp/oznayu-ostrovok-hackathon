"""Контракт 3: разобранный запрос гостя (AST). Схема для сервера; компактная схема для модели —
задача Данила (у Claude ограничения на union-поля и рекурсию, см. docs/research/review_gpt_v4b.md).

LLM переводит текст в эту структуру, сервер проверяет её этой моделью. Ни одна часть запроса
не теряется молча: если для неё нет характеристики — она идёт в `out_of_catalog` (D34).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_PATH = Path(__file__).parent / "schemas" / "query_ast.schema.json"

Op = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "exists"]


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(description="ID характеристики каталога, например V05 или W01")
    op: Op
    value: Any = None
    field: str | None = Field(
        default=None, description="Какое поле значения сравнивать: distance_m, count, mean…"
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Параметры: месяц, цель маршрута, время приезда"
    )
    binding: Literal["same_placement", "hotel"] = Field(
        default="same_placement",
        description="Условия уровня номера проверяются на одном размещении (D17)",
    )
    quantifier: Literal["all", "any"] = "all"
    text_span: str | None = Field(default=None, description="Фрагмент запроса, откуда условие")


class SoftCondition(Condition):
    weight: float = Field(gt=0, le=1)


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    about: str = Field(description="Какую неоднозначность снимает: «тихо» — в номере или у дорог")
    options: list[str] = Field(default_factory=list)


class OutOfCatalog(BaseModel):
    """Часть запроса без характеристики в каталоге (D34): показываем «такое мы пока не знаем»."""

    model_config = ConfigDict(extra="forbid")

    text_span: str
    normalized: str
    kind: Literal["missing_feature", "subjective", "unsupported_param"] = "missing_feature"


class Guests(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adults: int | None = None
    children_ages: list[int] = Field(default_factory=list)
    pets: list[str] = Field(default_factory=list)


class Dates(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: date | None = Field(default=None, alias="from")
    to: date | None = None


class QueryAST(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str
    city_id: str | None = None
    dates: Dates | None = None
    guests: Guests | None = None
    hard: list[Condition] = Field(default_factory=list)
    soft: list[SoftCondition] = Field(default_factory=list)
    clarify: list[Clarification] = Field(default_factory=list)
    out_of_catalog: list[OutOfCatalog] = Field(default_factory=list)
    parsed_by: Literal["llm", "rules", "stub"] = "llm"


def json_schema() -> dict[str, Any]:
    return QueryAST.model_json_schema(by_alias=True)


def write_schema(path: Path = SCHEMA_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    write_schema()
