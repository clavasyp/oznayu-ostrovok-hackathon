"""Проверка черновика спецификации (core §7.4, шаг 2): калькуляторы и слои существуют, единицы и
уровень согласованы, поиск ссылается на поле значения, такой характеристики ещё нет.

`problems` — черновик нельзя гонять и включать, пока не исправят. `warnings` — можно, но человеку
стоит посмотреть (похожая характеристика уже есть, калькулятор ещё не зарегистрирован).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agents.catalog import load_catalog
from agents.search.text import normalize
from agents.specgen.registry import (
    CALCULATORS,
    LAYERS,
    WORLDCOVER_CLASSES,
    parse_ref,
    registered,
)
from agents.specgen.spec import FeatureSpec

UNITS = frozenset(
    {"m", "km", "min", "h", "%", "dB(A)", "mm", "kg", "m2", "°C", "count", "years", "days",
     "RUB", "Mbit/s", "ms", "kW"}
)  # fmt: skip
OPS_BY_TYPE = {
    "number": {"eq", "ne", "lt", "lte", "gt", "gte", "exists"},
    "integer": {"eq", "ne", "lt", "lte", "gt", "gte", "exists"},
    "interval": {"lt", "lte", "gt", "gte", "exists"},
    "bool": {"eq", "ne", "exists"},
    "enum": {"eq", "ne", "in", "exists"},
    "string": {"eq", "ne", "exists"},
}
ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
RADIUS_M = (100, 2000)


def _same(a: dict, b: dict) -> bool:
    def norm(v):
        return sorted(v) if isinstance(v, list) else v

    return a.keys() == b.keys() and all(norm(a[k]) == norm(b[k]) for k in a)


@dataclass
class Check:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trial_ready: bool = False  # есть рецепт с зарегистрированным калькулятором и нет проблем


def check(spec: FeatureSpec) -> Check:
    out = Check()
    catalog = load_catalog()
    plugins = registered()
    p, w = out.problems.append, out.warnings.append

    if not ID.match(spec.id):
        p(f"id «{spec.id}»: латиница в нижнем регистре, цифры и «_», 3–64 знака")
    taken = catalog.get(spec.id.upper()) or catalog.get(spec.id)
    if taken and not taken.runtime:  # включённую текстом можно включить снова — пересчёт
        p(f"id «{spec.id}» уже занят характеристикой каталога")
    synonyms = {normalize(s).strip() for s in spec.search.synonyms}
    for f in catalog.features.values():
        if f.id == spec.id:
            continue
        same = synonyms & {normalize(s).strip() for s in f.synonyms}
        if same:
            w(f"похоже на {f.id} «{f.short}»: те же слова гостя ({', '.join(sorted(same))})")

    if not spec.value:
        p("нет полей значения (value)")
    for name, v in spec.value.items():
        if v.unit and v.unit not in UNITS:
            p(f"value.{name}: единица «{v.unit}» неизвестна (есть: {', '.join(sorted(UNITS))})")
        if v.type in ("number", "integer", "interval") and not v.unit and v.type != "integer":
            w(f"value.{name}: число без единицы")
        if v.type == "enum" and not v.values:
            p(f"value.{name}: у enum нет списка значений")
    default = spec.value.get(spec.search.default_field)
    if default is None:
        p(f"search.default_field «{spec.search.default_field}» — нет такого поля в value")
    else:
        bad = set(spec.search.operators) - OPS_BY_TYPE[default.type]
        if bad:
            p(f"search.operators {sorted(bad)} не подходят к типу {default.type}")
    if not spec.search.operators:
        p("search.operators пуст: гость не сможет спросить")

    if not spec.recipes:
        p("нет ни одного рецепта")
    ids = [r.id for r in spec.recipes]
    if len(set(ids)) != len(ids):
        p("id рецептов повторяются")
    for r in spec.recipes:
        where = f"рецепт {r.id}"
        if r.kind == "hotelier":
            if not (r.question or "").strip():
                p(f"{where}: у вопроса отельеру нет текста")
            if not r.parse_to or set(r.parse_to) - set(spec.value):
                p(f"{where}: parse_to должен ссылаться на поля value")
            continue
        if r.kind != "computed":
            continue
        layers = r.requires.layers if r.requires else []
        for layer in layers:
            if layer not in LAYERS:
                p(f"{where}: слоя «{layer}» нет (есть: {', '.join(LAYERS)})")
        if not r.calculator:
            p(f"{where}: не указан калькулятор")
            continue
        name, version = parse_ref(r.calculator)
        calc = CALCULATORS.get(name)
        if calc is None:
            p(f"{where}: калькулятора «{name}» нет (есть: {', '.join(CALCULATORS)})")
            continue
        if version and version != calc.version:
            p(f"{where}: у {name} версия {calc.version}, а не {version}")
        if calc.subject_level != spec.subject_level:
            p(f"{where}: {name} считает уровень {calc.subject_level}, а не {spec.subject_level}")
        if missing := set(calc.layers) - set(layers):
            w(f"{where}: {name} читает слои {sorted(missing)} — добавьте в requires.layers")
        if default is not None and default.unit != calc.unit:
            p(f"{where}: {name} даёт «{calc.unit}», а поле {spec.search.default_field} — "
              f"«{default.unit}»")  # fmt: skip
        if extra := set(r.params) - set(calc.params) - set(calc.fixed):
            p(f"{where}: у {name} нет параметров {sorted(extra)}")
        for k, fixed in calc.fixed.items():
            if k in r.params and r.params[k] != fixed:
                p(f"{where}: у {name} параметр {k} фиксирован ({fixed})")
        # То же, что уже считает калькулятор с фиксированными параметрами, — та же характеристика:
        # «доля зелени в 300 м» = E03.
        for other in CALCULATORS.values():
            effective = {**calc.fixed, **r.params}
            if other.fixed and other.layers == calc.layers and _same(effective, other.fixed):
                plugin = plugins.get(other.id)
                same = ", ".join(plugin.feature_ids) if plugin else other.id
                p(f"{where}: такая характеристика уже есть — {same} (калькулятор {other.id})")
        if "classes" in r.params:
            classes = r.params["classes"]
            codes = ", ".join(map(str, WORLDCOVER_CLASSES))
            if (
                not isinstance(classes, list)
                or not classes
                or set(classes) - set(WORLDCOVER_CLASSES)
            ):
                p(f"{where}: classes — список кодов WorldCover ({codes})")
        if "radius_m" in r.params:
            radius = r.params["radius_m"]
            if not isinstance(radius, int | float) or not RADIUS_M[0] <= radius <= RADIUS_M[1]:
                p(f"{where}: radius_m — число от {RADIUS_M[0]} до {RADIUS_M[1]} м")
        if name == "geo.place_count":
            from agents.catalog.places import load_places

            if load_places().get(str(r.params.get("category"))) is None:
                p(f"{where}: category — ID из словаря мест agents/catalog/places.json")
            # Места карт — не перепись: число «не меньше», «не больше N» по нему не обещаем.
            if default is not None and set(spec.search.operators) - {"gte", "gt", "exists"}:
                p(f"{where}: {name} даёт число «не меньше» — в search.operators только gte, gt")
        if name not in plugins:
            w(f"{where}: калькулятор {name} ещё не зарегистрирован ({calc.owner}) — пробный "
              "расчёт недоступен")  # fmt: skip
    for rel in spec.related:
        if catalog.get(rel) is None:
            w(f"related: «{rel}» нет в каталоге")

    out.trial_ready = not out.problems and any(
        r.kind == "computed" and r.calculator and parse_ref(r.calculator)[0] in plugins
        for r in spec.recipes
    )
    return out
