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
Mapping = Literal["exact", "partial", "different_meaning"]


class Span(BaseModel):
    """Фрагмент исходного текста: позиции в Unicode code points, полуинтервал [start, end) (v11)."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str


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
    atom_id: str | None = Field(default=None, description="ID атома запроса (v11: AST без потерь)")
    span: Span | None = None
    provenance: Literal["explicit", "inferred"] = Field(
        default="explicit", description="Явно сказано или выведено (выведенное — только уточнение)"
    )
    mapping: Mapping = Field(
        default="exact", description="Насколько смысл атома совпадает с характеристикой"
    )


class SoftCondition(Condition):
    weight: float = Field(gt=0, le=1)


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    about: str = Field(description="Какую неоднозначность снимает: «тихо» — в номере или у дорог")
    options: list[str] = Field(default_factory=list)


class NearbyPlace(BaseModel):
    """«Рядом место типа X» (задача 4): категория из `agents/catalog/places.json` и радиус.

    Проверить можно, когда в пакете города будет таблица «отель × места рядом» (метки OSM,
    рубрики 2ГИС); до тех пор часть запроса — «вне каталога», но уже с категорией.
    """

    model_config = ConfigDict(extra="forbid")

    category: str = Field(description="ID категории из словаря мест")
    label: str
    radius_m: int | None = Field(default=None, gt=0, description="Пешком; None — место в отеле")
    radius_source: Literal["guest", "policy"] | None = Field(
        default=None, description="guest — гость назвал расстояние; policy — «рядом» = near_m"
    )
    on_site: bool = Field(default=False, description="«в отеле», «отель с рестораном»")
    quality: str | None = Field(
        default=None,
        description="«хорошие», «лучшие», «вкусные» — слова гостя о качестве; проверить нечем",
    )


class OutOfCatalog(BaseModel):
    """Часть запроса без характеристики в каталоге (D34): показываем «такое мы пока не знаем»."""

    model_config = ConfigDict(extra="forbid")

    text_span: str
    normalized: str
    kind: Literal["missing_feature", "subjective", "unsupported_param"] = "missing_feature"
    atom_id: str | None = None
    span: Span | None = None
    requiredness: Literal["required", "desired", "unspecified"] = "unspecified"
    mapping_status: Literal["partial", "different_meaning", "unmatched"] = "unmatched"
    nearest_contracts: list[str] = Field(
        default_factory=list, description="Ближайшие характеристики каталога, если есть"
    )
    place: NearbyPlace | None = Field(default=None, description="Для `nearby_place`: что и где")


class LogicNode(BaseModel):
    """Узел логики запроса (v11).

    ATOM ссылается на условие по atom_id; AND / OR / NOT — на детей.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    op: Literal["ATOM", "AND", "OR", "NOT"]
    atom_id: str | None = None
    children: list[str] = Field(default_factory=list)


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
    logic: list[LogicNode] = Field(
        default_factory=list,
        description="Необязательно: граф AND / OR / NOT; пусто — все hard соединены через AND",
    )
    hard_root: str | None = Field(default=None, description="Корень графа обязательных условий")
    unparsed_spans: list[Span] = Field(
        default_factory=list, description="Неразобранные фрагменты — не теряются молча"
    )
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
