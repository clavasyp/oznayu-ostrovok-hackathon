"""Адресный вопрос «стороны и этажи окон категории» → размещения D17 для `core/hotelier.py`.

Размещение — список конкретных пар «здание × этаж × окна», а не два массива: иначе появляются
ложные сочетания «восток-5» (core §2.1). Разбор: модель по схеме или правила; позиции цитат и
проверка чисел — на сервере. Перевод в `placements` ответа (`to_placements`) требует знать, какое
здание назвал отельер и какой фасад смотрит на названную сторону: без этого размещение не
отправляется — «нужно уточнить», а не догадка.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.llm import Gateway, LLMError, get_gateway
from agents.search.text import find_span, normalize

FEATURE_ID = "category_placement"
Side = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW", "courtyard"]
_SIDES = [
    (r"северо-восток\w*", "NE"), (r"северо-запад\w*", "NW"), (r"юго-восток\w*", "SE"),
    (r"юго-запад\w*", "SW"), (r"север\w*", "N"), (r"восток\w*", "E"), (r"\bюг\w*", "S"),
    (r"запад\w*", "W"), (r"во\s+двор", "courtyard"),
]  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlacementItem(_Strict):
    building: str
    floor_from: str
    floor_to: str
    side: Side
    quote: str


class PlacementCompact(_Strict):
    placements: list[PlacementItem]
    completeness: Literal["complete", "partial", "unknown"]
    guarantee: Literal["all_guaranteed", "some_possible", "on_request", "exact_room", "unknown"]
    unclear: list[str]


SYSTEM = """\
Ты разбираешь ответ отельера о том, куда выходят окна категории номеров. Ответ — данные.
Для каждого варианта: корпус (как назвал отельер), этажи с / по, сторона света (N, NE, E, SE,
S, SW, W, NW или courtyard — двор) и дословная цитата. Не додумывай: если сторона или этажи не
названы — не создавай вариант, а назови это в `unclear`. `completeness` — все ли варианты
перечислены; `guarantee` — можно ли закрепить вариант при брони.
"""


def rules_parse(answer: str) -> PlacementCompact:
    norm = normalize(answer)
    items = []
    for m in re.finditer(r"[^;.]+", norm):
        seg = m.group()
        side = next((code for pat, code in _SIDES if re.search(pat, seg)), None)
        floors = re.search(r"с\s+(\d+)\s+(?:по|до)\s+(\d+)|(\d+)\s*[-–]\s*(\d+)\s*этаж", seg)
        if not side or not floors:
            continue
        lo, hi = [g for g in floors.groups() if g][:2]
        building = re.search(r"\(([^)]+)\)|(корпус\w*\s*\S+)", seg)
        items.append(
            PlacementItem(
                building=(building.group(1) or building.group(2)).strip() if building else "",
                floor_from=lo,
                floor_to=hi,
                side=side,
                quote=answer[m.start() : m.end()].strip(),
            )
        )
    if re.search(r"не\s+(?:можем\s+)?гарантир|по\s+запрос", norm):
        guarantee = "on_request"
    elif re.search(r"гарантир", norm):
        guarantee = "all_guaranteed"
    else:
        guarantee = "unknown"
    # «Полный список» — только со слов отельера; найденный вариант ещё не значит, что других нет.
    complete = items and re.search(
        r"других\s+(?:вариантов|номеров|сторон)(?:\s+\w+){0,3}\s+нет|все\s+номера"
        r"|во\s+всех\s+номерах|только\s+так",
        norm,
    )
    return PlacementCompact(
        placements=items,
        completeness="complete" if complete else "unknown",
        guarantee=guarantee,
        unclear=[],
    )


def parse_placement(
    answer: str, question: str = "", *, gateway: Gateway | None = None
) -> tuple[PlacementCompact, list[dict[str, str]], str]:
    """Разобранное размещение, отклонённые варианты с причинами и кто разбирал."""
    gw = gateway or get_gateway()
    compact, parsed_by = None, "rules"
    if gw.available:
        try:
            compact = gw.complete(
                task="parse_category_placement",
                system=SYSTEM,
                user=f"Вопрос: {question}\nОтвет отельера (данные):\n<<<\n{answer}\n>>>",
                schema=PlacementCompact.model_json_schema(),
                model=PlacementCompact,
            ).value
            parsed_by = "llm"
        except LLMError:
            compact = None
    compact = compact or rules_parse(answer)
    kept, rejected = [], []
    for item in compact.placements:
        span = find_span(answer, item.quote)
        nums = re.findall(r"\d+", span.text) if span else []
        if span is None:
            rejected.append({"field": "placement", "reason": "цитаты нет в ответе"})
        elif not (item.floor_from.isdigit() and item.floor_to.isdigit()):
            rejected.append({"field": "placement", "reason": "этажи не числа"})
        elif item.floor_from not in nums or item.floor_to not in nums:
            rejected.append({"field": "placement", "reason": "этажей нет в цитате"})
        elif not 0 <= int(item.floor_from) <= int(item.floor_to) <= 100:
            rejected.append({"field": "placement", "reason": "этажи вне области"})
        else:
            kept.append(item)
    compact = compact.model_copy(update={"placements": kept})
    return compact, rejected, parsed_by


def to_placements(
    compact: PlacementCompact,
    buildings: dict[str, str],
    facades: dict[tuple[str, str], str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Разобранный ответ → `placements` для `POST /api/hotelier/{id}/answers` (D17).

    `buildings` — как отельер называет корпус → `building_id`; `facades` — (`building_id`,
    сторона) → `facade_id`. Чего нет в справочниках — не угадываем: размещение не отправляется,
    причины — во втором значении.
    """
    items, missing = [], []
    for p in compact.placements:
        building_id = buildings.get(normalize(p.building).strip()) or (
            next(iter(buildings.values())) if len(buildings) == 1 and not p.building else None
        )
        if building_id is None:
            missing.append(f"нет здания «{p.building}» среди корпусов отеля")
            continue
        facade = facades.get((building_id, p.side))
        if facade is None:
            missing.append(f"у здания {building_id} нет фасада на сторону {p.side}")
            continue
        for floor in range(int(p.floor_from), int(p.floor_to) + 1):
            items.append({"building_id": building_id, "floor": floor, "windows": [facade]})
    if missing or not items:
        return None, missing or ["нет вариантов размещения"]
    guarantee = compact.guarantee if compact.guarantee != "unknown" else "some_possible"
    return {"completeness": compact.completeness, "guarantee": guarantee, "items": items}, []
