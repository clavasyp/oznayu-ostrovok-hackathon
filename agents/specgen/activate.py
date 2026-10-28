"""Включение характеристики, описанной текстом (core §7.4, шаг 4; задача 4 трека).

Черновик, который человек проверил пробным расчётом, становится частью системы без правки кода:
калькулятор из реестра считает её для всех отелей городов, факты с паспортами пишутся в базу,
запись попадает в каталог (`agents.catalog.register_features`) — разбор узнаёт её по синонимам,
поиск сравнивает по фактам, покрытие видно сразу. Включает человек: агент-исследователь только
предлагает черновик (D33).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from agents.catalog import register_features
from agents.specgen.registry import parse_ref, registered
from agents.specgen.spec import FeatureSpec, to_yaml
from agents.specgen.trial import TIMEZONES, subjects_of
from agents.specgen.validate import check
from core.plugins import CityContext

FOCUS_CITIES = ("moscow", "sochi")  # D36: глубина — Москва и Большой Сочи


def spec_feature(spec: FeatureSpec) -> dict[str, Any]:
    """Спецификация → запись каталога в формате `features.json`."""
    title = spec.title
    return {
        "id": spec.id,
        "title": title,
        "short": title[:1].lower() + title[1:],
        "level": spec.subject_level,
        "source": "computed",
        "fields": {
            name: {"type": v.type, "unit": v.unit, "values": v.values or []}
            for name, v in spec.value.items()
        },
        "default_field": spec.search.default_field,
        "ops": list(spec.search.operators),
        "params": [],
        "synonyms": list(spec.search.synonyms),
        "search_default": spec.search.default.model_dump() if spec.search.default else None,
        "spec_version": spec.version,
    }


def activate(
    spec: FeatureSpec, store: Any, cities: tuple[str, ...] = FOCUS_CITIES, data_dir: Path = Path()
) -> dict[str, Any]:
    """Посчитать характеристику для всех отелей городов, сохранить факты и включить в каталог."""
    result = check(spec)
    if result.problems:
        return {"activated": False, "spec_id": spec.id, "problems": result.problems}
    plugins = registered()
    recipe = next(
        (r for r in spec.recipes
         if r.kind == "computed" and r.calculator and parse_ref(r.calculator)[0] in plugins),
        None,
    )  # fmt: skip
    if recipe is None:
        return {"activated": False, "spec_id": spec.id,
                "problems": ["нет рецепта с зарегистрированным калькулятором"]}  # fmt: skip
    calc = plugins[parse_ref(recipe.calculator)[0]]
    facts, by_city = [], {}
    for city in cities:
        subjects = subjects_of(store.hotels(city))
        ctx = CityContext(
            city_id=city,
            timezone=TIMEZONES.get(city, "Europe/Moscow"),
            data_dir=data_dir,
            params=dict(recipe.params) | {"feature_id": spec.id, "store": store},
        )
        got = calc.run(subjects, ctx)
        statuses = Counter(str(f.status) for f in got)
        known = statuses.get("computed", 0) + statuses.get("observed", 0)
        by_city[city] = {
            "hotels": len(subjects),
            "computed": known,
            "coverage": round(known / len(subjects), 3) if subjects else 0.0,
            "unknown_reasons": dict(
                Counter(f.unknown_reason for f in got if f.unknown_reason).most_common(3)
            ),
        }
        facts += got
    active = spec.model_copy(update={"status": "active"})
    feature = spec_feature(active)
    store.save_feature(feature, to_yaml(active), facts, by_city, spec.source_text)
    register_features([feature])
    return {
        "activated": True,
        "spec_id": spec.id,
        "title": spec.title,
        "calculator": f"{calc.id}@{calc.version}",
        "facts": len(facts),
        "cities": by_city,
        "warnings": result.warnings,
    }
