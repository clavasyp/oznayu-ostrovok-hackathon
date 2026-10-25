"""Противоречие «наш расчёт ↔ ответ отеля» → уточняющий вопрос отельеру (core §6, задача 4).

Победителя не назначаем: противоречие уже записано (core/hotelier.py, журнал противоречий), поиск
показывает «нужно уточнить». Агент лишь формулирует вопрос: оба значения рядом, источник нашего,
просьба сказать, что верно сейчас и не различается ли по корпусам или номерам. Значения — из
фактов как есть, модель тут не нужна.
"""

from __future__ import annotations

from typing import Any

from agents.catalog import load_catalog
from agents.hotelier.schemas import UNIT_RU, label
from core.facts import Fact

_SOURCE = {
    "computed": "по нашему расчёту",
    "observed": "по открытым данным",
    "curated": "по данным, проверенным командой",
}


def _show(value: Any, unit: str | None) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    shown = f"{value:g}".replace(".", ",") if isinstance(value, float) else str(value)
    return f"{shown} {UNIT_RU.get(unit or '', unit or '')}".strip()


def _source(fact: Fact) -> str:
    kind = _SOURCE.get(str(fact.status), str(fact.status))
    providers = sorted({s.provider for s in fact.sources if s.provider})
    return f"{kind} ({', '.join(providers)})" if providers else kind


def conflict_question(ours: Fact, theirs: Fact) -> dict[str, Any]:
    """Пара фактов из `AnswerResult.conflicts` (наш расчёт, ответ отеля) → вопрос отельеру."""
    feature = load_catalog().get(ours.feature_id)
    what = feature.short if feature else ours.feature_id
    source = _source(ours)
    if isinstance(ours.value, dict) and isinstance(theirs.value, dict):
        # Только поля, где значения разошлись: остальное в ответе отеля не оспаривается.
        fields = [k for k in ours.value if k in theirs.value and ours.value[k] != theirs.value[k]]
        specs = feature.fields if feature else {}
        pairs = []
        for name in fields:
            unit = specs[name].unit if name in specs else None
            pairs.append(
                f"{label(name).split(',')[0]} — в вашем ответе {_show(theirs.value[name], unit)}, "
                f"а {source} {_show(ours.value[name], unit)}"
            )
        detail = "; ".join(pairs)
    else:
        fields = []
        detail = (
            f"в вашем ответе {_show(theirs.value, theirs.unit)}, "
            f"а {source} {_show(ours.value, ours.unit)}"
        )
    text = (
        f"Уточните, пожалуйста, «{what}»: {detail}. Что верно сейчас? "
        "Если по корпусам или номерам по-разному — напишите, где как."
    )
    return {
        "feature_id": ours.feature_id,
        "subject_id": ours.subject_id,
        "fields": fields,
        "fact_ids": [ours.fact_id, theirs.fact_id],
        "text": text,
    }
