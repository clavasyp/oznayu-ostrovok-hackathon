"""Что есть для новой характеристики (core §7.4): слои данных и калькуляторы.

Модель видит этот список и может ссылаться только на него; проверка (`validate.py`) сверяет
черновик с ним же. Калькуляторы берутся из реестра плагинов (`core.plugins.CALCULATORS`), а их
параметры, слои и единица — из описания ниже. Калькулятор, которого ещё нет в реестре, может
стоять в черновике (`planned`), но пробный расчёт по нему недоступен — это честно видно админу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Классы ESA WorldCover 2021 (v200): код → смысл. Растительность E03 — 10, 20, 30, 40, 90, 95, 100.
WORLDCOVER_CLASSES = {
    10: "деревья",
    20: "кустарник",
    30: "травы и луга",
    40: "пашня",
    50: "застройка",
    60: "голая земля",
    70: "снег и лёд",
    80: "вода",
    90: "водно-болотные угодья",
    95: "мангры",
    100: "мхи и лишайники",
}


@dataclass(frozen=True)
class Layer:
    id: str
    title: str
    provider: str
    license: str
    kind: str  # raster | vector | timetable
    notes: str = ""


LAYERS: dict[str, Layer] = {
    layer.id: layer
    for layer in (
        Layer("worldcover", "Покров земли 10 м, 2021", "ESA WorldCover", "CC BY 4.0", "raster",
              "классы: " + ", ".join(f"{k} {v}" for k, v in WORLDCOVER_CLASSES.items())),
        Layer("osm_buildings", "Контуры зданий, этажность", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("osm_paths", "Пешеходный граф", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("osm_roads", "Дороги по классам", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("osm_rail", "Железные дороги", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("osm_poi", "Места с метками и часами работы", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("beach", "Пляжи и входы на пляж", "OpenStreetMap", "ODbL 1.0", "vector"),
        Layer("dem", "Рельеф 30 м", "Copernicus DEM GLO-30", "Copernicus licence", "raster"),
        Layer("gtfs", "Расписание общественного транспорта", "GTFS", "по источнику", "timetable"),
    )
}  # fmt: skip


@dataclass(frozen=True)
class CalculatorSpec:
    id: str
    version: str  # как в спецификации: «env.landcover_share@1» → "1"
    title: str
    subject_level: str
    layers: tuple[str, ...]
    unit: str
    params: dict[str, str] = field(default_factory=dict)  # параметр → что значит
    fixed: dict[str, Any] = field(default_factory=dict)  # параметры, которые не меняются
    owner: str = ""


# Описание калькуляторов для черновика: параметры, слои, единица. `registered` — есть ли он в
# реестре плагинов (`core.plugins`): по незарегистрированному пробный расчёт недоступен.
CALCULATORS: dict[str, CalculatorSpec] = {
    c.id: c
    for c in (
        CalculatorSpec(
            id="env.vegetation_share",
            version="1",
            title="Доля растительности на суше в радиусе 300 м (E03)",
            subject_level="hotel",
            layers=("worldcover",),
            unit="%",
            fixed={"radius_m": 300, "classes": [10, 20, 30, 40, 90, 95, 100]},
            owner="Лера",
        ),
        CalculatorSpec(
            id="env.landcover_share",
            version="1",
            title="Доля классов покрова WorldCover на суше в радиусе вокруг отеля",
            subject_level="hotel",
            layers=("worldcover",),
            unit="%",
            params={
                "classes": "список кодов WorldCover, например [50] — застройка",
                "radius_m": "радиус круга вокруг отеля, м (100–2000)",
            },
            owner="Лера (план, docs/tasks/lera.md)",
        ),
    )
}


def registered() -> dict[str, Any]:
    """Калькуляторы, зарегистрированные в плагинах сейчас (импорт `calculators` их регистрирует)."""
    import calculators  # noqa: F401  — регистрация при импорте
    from core.plugins import CALCULATORS as PLUGINS

    return dict(PLUGINS)


def parse_ref(ref: str) -> tuple[str, str | None]:
    """«env.landcover_share@1» → ("env.landcover_share", "1")."""
    name, _, version = ref.partition("@")
    return name.strip(), version.strip() or None


def listing() -> str:
    """Список для промпта модели: слои и калькуляторы с параметрами."""
    plugins = registered()
    lines = ["Слои (id | что | источник, лицензия):"]
    lines += [f"- {x.id} | {x.title}{'; ' + x.notes if x.notes else ''} | {x.provider}, "
              f"{x.license}" for x in LAYERS.values()]  # fmt: skip
    lines.append("Калькуляторы (ссылка | что | уровень | слои | единица | параметры):")
    for c in CALCULATORS.values():
        params = "; ".join(f"{k}: {v}" for k, v in c.params.items()) or (
            "фиксированы: " + ", ".join(f"{k}={v}" for k, v in c.fixed.items())
        )
        state = "" if c.id in plugins else " (ещё не зарегистрирован)"
        lines.append(f"- {c.id}@{c.version}{state} | {c.title} | {c.subject_level} | "
                     f"{', '.join(c.layers)} | {c.unit} | {params}")  # fmt: skip
    return "\n".join(lines)
