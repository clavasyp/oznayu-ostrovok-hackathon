"""Адресный вопрос «стороны и этажи окон категории» → факт размещения (D17).

Размещение — список конкретных пар «корпус × этажи × сторона», а не два массива: иначе
появляются ложные сочетания (core §2.1). Разбор: модель по схеме или правила; позиции цитат и
проверка чисел — на сервере. Результат — факт `category_placement` уровня категории: по нему
лид считает вид и солнце по этажам («закат виден с 5-го этажа»).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.llm import Gateway, LLMError, get_gateway
from agents.search.text import find_span, normalize
from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id

FEATURE_ID = "category_placement"
ALGORITHM = "hotelier_placement@1"
Side = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW", "courtyard"]
AZIMUTH = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
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
    return PlacementCompact(
        placements=items,
        completeness="complete" if items else "unknown",
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


def placement_fact(
    *,
    hotel_id: str,
    city_id: str,
    room_type_id: str,
    compact: PlacementCompact,
    question_id: str,
    is_simulated: bool,
    parsed_by: str,
    at: datetime | None = None,
) -> Fact | None:
    if not compact.placements:
        return None
    at = at or datetime.now(UTC)
    algorithm = ALGORITHM + (":simulated" if is_simulated else "")
    placements: list[dict[str, Any]] = [
        {
            "building": p.building or None,
            "floors": list(range(int(p.floor_from), int(p.floor_to) + 1)),
            "side": p.side,
            "azimuth_deg": AZIMUTH.get(p.side),
        }
        for p in compact.placements
    ]
    return Fact(
        fact_id=make_fact_id(hotel_id, room_type_id, FEATURE_ID, {}, algorithm),
        hotel_id=hotel_id,
        city_id=city_id,
        subject_level="room_type",
        subject_id=room_type_id,
        feature_id=FEATURE_ID,
        value={
            "placements": placements,
            "completeness": compact.completeness,
            "guarantee": compact.guarantee,
        },
        status=Status.simulated_hotelier if is_simulated else Status.hotelier,
        sources=[Source(provider="hotelier_simulator" if is_simulated else "hotelier")],
        recipe_id="ask_hotelier",
        algorithm_version=algorithm,
        confidence=Confidence(level="medium", reason="ответ отельера о размещении категории"),
        observed_at=at,
        calculated_at=at,
        recipe_attempts=[RecipeAttempt(recipe_id="ask_hotelier", state="accepted", data_kind="В")],
        # Без гарантии варианта размещение не обещает этаж и сторону конкретной брони (D17).
        hard_condition_eligible=compact.guarantee == "all_guaranteed",
        is_simulated=is_simulated,
        question_id=question_id,
        quotes=[p.quote for p in compact.placements],
        parsed_by=parsed_by,
    )
