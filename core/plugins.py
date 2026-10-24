"""Контракт 2: интерфейс плагинов — калькуляторов и коннекторов (core.md §4–§5).

Калькулятор получает объекты (отели, корпуса, маршруты) и контекст города и возвращает факты
с паспортом. В каждом факте — `recipe_attempts`: какие рецепты пробовали и почему отклонили
(права, поле, покрытие, качество, другой смысл). Неудача рецепта — тоже результат:
факт `unknown` с причиной, а не исключение и не пропуск.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.facts import Fact


@dataclass(frozen=True)
class DatasetRef:
    """Версия входного набора: что именно лежит на диске и откуда."""

    id: str  # например "osm_pbf:southern-fed-district"
    path: Path
    provider: str
    version: str | None = None
    sha256: str | None = None
    license: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class CityContext:
    city_id: str
    timezone: str
    data_dir: Path
    datasets: dict[str, DatasetRef] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)  # даты поездок, цели маршрутов и т. п.


@dataclass(frozen=True)
class Subject:
    """Объект расчёта. Для уровня hotel subject_id = "hotel:<masterId>", как в v9."""

    hotel_id: str
    subject_level: str
    subject_id: str
    latitude: float | None = None
    longitude: float | None = None
    geometry_wkt: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Calculator(Protocol):
    id: str  # "env.vegetation_share"
    version: str  # версия алгоритма → Fact.algorithm_version
    feature_ids: tuple[str, ...]  # какие характеристики каталога выдаёт

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]: ...


@runtime_checkable
class Connector(Protocol):
    id: str  # "osm_pbf", "ostrovok_site"
    source_id: str
    layers: tuple[str, ...]

    def available(self, ctx: CityContext) -> bool: ...

    def fetch(self, ctx: CityContext, workdir: Path) -> list[DatasetRef]: ...


CALCULATORS: dict[str, Calculator] = {}
CONNECTORS: dict[str, Connector] = {}


def register_calculator[T: Calculator](cls: Callable[[], T]) -> Callable[[], T]:
    instance = cls()
    if instance.id in CALCULATORS:
        raise ValueError(f"калькулятор {instance.id} уже зарегистрирован")
    CALCULATORS[instance.id] = instance
    return cls


def register_connector[T: Connector](cls: Callable[[], T]) -> Callable[[], T]:
    instance = cls()
    if instance.id in CONNECTORS:
        raise ValueError(f"коннектор {instance.id} уже зарегистрирован")
    CONNECTORS[instance.id] = instance
    return cls


def calculators_for(feature_id: str) -> list[Calculator]:
    return [c for c in CALCULATORS.values() if feature_id in c.feature_ids]
