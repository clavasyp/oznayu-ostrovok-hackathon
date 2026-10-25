"""Словарь мест для «рядом место типа X» (`places.json`, задача 4, D34, D37).

Слова гостя → категория → метки OSM (и рубрики 2ГИС, когда их даст Сева). Здесь только смысл:
мест и расстояний нет — их соберёт пакет города в таблицу «отель × места рядом». Пока таблицы
нет, часть запроса остаётся «вне каталога», но уже с категорией и радиусом.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PLACES_PATH = Path(__file__).parent / "places.json"


@dataclass(frozen=True)
class PlaceCategory:
    id: str
    label: str
    patterns: tuple[re.Pattern[str], ...]
    osm: tuple[dict[str, tuple[str, ...]], ...]  # варианты через ИЛИ, ключи варианта — через И
    exclude: dict[str, tuple[str, ...]]
    twogis: tuple[str, ...]
    needs_near: bool  # «бассейн», «ресторан» — обычно про сам отель; берём только с «рядом»


@dataclass(frozen=True)
class Places:
    version: int
    near_m: int  # «рядом», «недалеко» без числа — политика, как «недалеко от моря»
    categories: dict[str, PlaceCategory]

    def get(self, category_id: str) -> PlaceCategory | None:
        return self.categories.get(category_id.strip())

    def find(self, norm: str) -> list[tuple[int, int, PlaceCategory]]:
        """Места в тексте (нижний регистр, «е» вместо «ё»): без пересечений, длинное — раньше."""
        found = [
            (m.start(), m.end(), c)
            for c in self.categories.values()
            for p in c.patterns
            for m in p.finditer(norm)
        ]
        found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
        kept: list[tuple[int, int, PlaceCategory]] = []
        for f in found:
            if all(f[1] <= k[0] or f[0] >= k[1] for k in kept):
                kept.append(f)
        return sorted(kept, key=lambda f: f[0])

    def osm_filters(self) -> list[dict[str, Any]]:
        """Все фильтры меток — для сборки таблицы «отель × места рядом» (пакет города, Сева)."""
        return [
            {"category": c.id, "any_of": [dict(v) for v in c.osm], "exclude": dict(c.exclude)}
            for c in self.categories.values()
        ]


@lru_cache(maxsize=1)
def load_places(path: Path = PLACES_PATH) -> Places:
    doc = json.loads(path.read_text("utf-8"))
    food = doc["food"]
    categories = {}
    for c in doc["categories"]:
        categories[c["id"]] = PlaceCategory(
            id=c["id"],
            label=c["label"],
            patterns=tuple(re.compile(w.replace("{food}", food)) for w in c["words"]),
            osm=tuple({k: tuple(v) for k, v in alt.items()} for alt in c["osm"]),
            exclude={k: tuple(v) for k, v in c.get("exclude", {}).items()},
            twogis=tuple(c.get("twogis", [])),
            needs_near=bool(c.get("needs_near", False)),
        )
    return Places(version=doc["version"], near_m=int(doc["near_m"]), categories=categories)


def matches(tags: dict[str, str], category: PlaceCategory) -> bool:
    """Подходит ли объект OSM с такими метками. Значения через «;» («italian;pizza») — каждое."""

    def values(key: str) -> set[str]:
        return {v.strip() for v in tags.get(key, "").split(";") if v.strip()}

    if any(values(k) & set(bad) for k, bad in category.exclude.items()):
        return False
    return any(
        all(values(k) and ("*" in ok or values(k) & set(ok)) for k, ok in alt.items())
        for alt in category.osm
    )
