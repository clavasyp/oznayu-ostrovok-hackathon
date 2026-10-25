"""Пробный расчёт черновика (core §7.4, шаг 3): калькулятор из реестра плагинов на 20 отелях.

Показывает покрытие — сколько посчитано, сколько неизвестно и почему, — чтобы админ решил,
включать ли характеристику. Факты пробного расчёта никуда не пишутся: включение (`active`) и
настоящий расчёт — после решения человека.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

from agents.specgen.registry import parse_ref, registered
from agents.specgen.spec import FeatureSpec
from agents.specgen.validate import check
from core.plugins import CityContext, Subject

TRIAL_N = 20
TIMEZONES = {
    "dubai": "Asia/Dubai",
    "istanbul": "Europe/Istanbul",
    "minsk": "Europe/Minsk",
    "kaliningrad": "Europe/Kaliningrad",
}  # остальные города показа — Europe/Moscow


def trial_run(
    spec: FeatureSpec,
    store: Any,
    city_id: str,
    *,
    n: int = TRIAL_N,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Прогон первого рецепта с зарегистрированным калькулятором на первых `n` отелях города."""
    result = check(spec)
    base = {"spec_id": spec.id, "city_id": city_id, "saved": False}
    if result.problems:
        return base | {"ran": False, "reason": "в черновике есть проблемы",
                       "problems": result.problems}  # fmt: skip
    plugins = registered()
    recipe = next(
        (r for r in spec.recipes
         if r.kind == "computed" and r.calculator and parse_ref(r.calculator)[0] in plugins),
        None,
    )  # fmt: skip
    if recipe is None:
        refs = [r.calculator for r in spec.recipes if r.calculator]
        return base | {
            "ran": False,
            "reason": "калькулятор ещё не зарегистрирован: " + (", ".join(refs) or "—"),
            "warnings": result.warnings,
        }
    calc = plugins[parse_ref(recipe.calculator)[0]]
    hotels = sorted(store.hotels(city_id), key=lambda h: str(h["hotel_id"]))[:n]
    subjects = [
        Subject(
            hotel_id=str(h["hotel_id"]),
            subject_level="hotel",
            subject_id=f"hotel:{h['hotel_id']}",
            latitude=h.get("latitude"),
            longitude=h.get("longitude"),
        )
        for h in hotels
        if h.get("latitude") is not None and h.get("longitude") is not None
    ]
    root = data_dir or Path(os.environ.get("DATA_DIR", "data"))
    params = dict(recipe.params)
    if os.environ.get("WORLDCOVER_DIR"):
        params.setdefault("worldcover_dir", os.environ["WORLDCOVER_DIR"])
    ctx = CityContext(
        city_id=city_id,
        timezone=TIMEZONES.get(city_id, "Europe/Moscow"),
        data_dir=root,
        params=params,
    )
    try:
        facts = calc.run(subjects, ctx)
    except Exception as e:  # калькулятор чужой: его сбой — результат пробного расчёта
        return base | {"ran": False, "reason": f"калькулятор {calc.id} упал: {e}"}
    statuses = Counter(str(f.status) for f in facts)
    reasons = Counter((f.unknown_reason or "").split(":")[0] for f in facts if f.unknown_reason)
    names = {str(h["hotel_id"]): h.get("name") for h in hotels}
    computed = statuses.get("computed", 0) + statuses.get("observed", 0)
    return base | {
        "ran": True,
        "calculator": f"{calc.id} ({calc.version})",
        "recipe_id": recipe.id,
        "hotels": len(subjects),
        "statuses": dict(statuses),
        "coverage": round(computed / len(subjects), 3) if subjects else 0.0,
        "unknown_reasons": dict(reasons.most_common(5)),
        "examples": [
            {
                "hotel_id": f.hotel_id,
                "name": names.get(f.hotel_id),
                "status": str(f.status),
                "value": f.value,
                "unit": f.unit,
            }
            for f in facts[:5]
        ],  # fmt: skip
        "warnings": result.warnings,
    }
