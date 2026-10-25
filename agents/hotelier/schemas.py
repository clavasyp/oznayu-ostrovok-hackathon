"""Поля ответа отельера: подписи, ключевые слова, допустимые области и схема для модели.

Типы и единицы полей — из словаря характеристик (`agents/catalog/features.json`): одно место
для поиска и для разбора ответа. Здесь — то, что нужно только разбору: как поле называть
человеку, по каким словам его узнать в свободном ответе и какие значения правдоподобны.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.catalog import Feature

# поле → (подпись, корни слов для поиска в ответе)
FIELDS: dict[str, tuple[str, str]] = {
    "has_ac": ("кондиционер в номере", r"кондиционер|кондей|охлажд"),
    "individual_control": (
        "гость сам управляет кондиционером",
        r"пульт|сам\w*\s+(?:включ|регул|настра)|управл",
    ),
    "operating_season": ("в какой период работает", r"круглый год|сезон|с\s+\w+\s+по"),
    "restrictions": ("ограничения", r"ограничен|только|кроме"),
    "full_blackout": ("полное затемнение", r"затемн|blackout|блэкаут|темнот|штор|ставн"),
    "coverage": ("насколько закрывают окна", r"полност|частичн|не\s+полност"),
    "edge_gaps": ("просветы по краям", r"просвет|по краям|щел"),
    "laeq_dba": ("измеренный уровень шума ночью, дБ(A)", r"дб|децибел|замер|измер"),
    "windows_state": ("окна при замере", r"окн"),
    "interval": ("интервал замера", r"с\s+\d|ночью|интервал"),
    "shared_wall_with_lift": (
        "общая стена с лифтовой шахтой",
        r"лифтов\w+\s+шахт|стен\w+\s+с\s+лифт",
    ),
    "assignment_guarantee": ("можно гарантировать при брони", r"гарант|закреп"),
    "own_parking": ("собственная парковка отеля", r"парковк|стоянк"),
    "reservation": ("нужно бронировать заранее", r"брон|заранее|резерв"),
    "fee_per_day": ("плата за парковку в сутки, ₽", r"руб|₽|бесплатн|сутк"),
    "max_height_m": ("максимальная высота машины, м", r"высот\w*\s+(?:авто|машин|до)|габарит"),
    "has_charger": ("зарядка для электромобиля", r"зарядк|зарядн"),
    "connector": ("разъём", r"type\s*2|тайп|ccs|chademo|gb/?t|разъ[её]м"),
    "power_kw": ("мощность, кВт", r"квт|kw|мощн"),
    "guest_access": ("доступна гостям", r"гост|доступ"),
    "checkin_possible": ("можно заселиться в это время", r"засел|заезд|при[её]м|встрет"),
    "front_desk_24h": ("круглосуточная стойка", r"круглосуточн|24/7|24 часа"),
    "self_checkin": ("самозаселение", r"самозасел|код|ключ\w*\s+в\s+(?:ящик|сейф)|бесконтакт"),
    "prior_arrangement": ("нужно предупредить заранее", r"предупред|заранее|согласов"),
    "breakfast_start": ("начало завтрака", r"завтрак"),
    "breakfast_box": ("завтрак с собой", r"с\s+собой|ланч-?бокс|breakfast box"),
    "cot_available": ("детская кроватка", r"кроватк"),
    "max_child_age": ("до какого возраста ребёнка, лет", r"лет|возраст"),
    "fee": ("доплата, ₽", r"руб|₽|бесплатн|доплат"),
    "connecting_rooms": ("смежные номера", r"смежн|соедин"),
    "guaranteed_pair": ("можно гарантировать пару номеров", r"гарант"),
    "kids_pool": ("детский бассейн", r"бассейн"),
    "minimum_age": ("с какого возраста, лет", r"лет|возраст"),
    "depth_m": ("глубина, м", r"глубин"),
    "supervised": ("присмотр персонала", r"присмотр|присматр|инструктор|спасател|персонал"),
    "pets_allowed": ("можно с животными", r"живот|питом|собак|кошк|кот"),
    "species": ("какие животные", r"собак|кошк|кот|живот"),
    "max_weight_kg": ("максимальная масса животного, кг", r"кг|килограм|вес|масс"),
    "max_number": (
        "сколько животных",
        r"одн\w+\s+(?:живот|питом|собак)|не\s+более\s+\d|до\s+\d+\s+(?:живот|питом)",
    ),
    "prior_approval": ("согласовать заранее", r"согласов|заранее|предупред"),
    "fee_amount": ("доплата за животное, ₽", r"руб|₽|доплат|бесплатн"),
    "fee_basis": ("за сутки или за всё проживание", r"сутк|ноч|за\s+(?:вс[её]|период|проживан)"),
    "deposit": ("депозит, ₽", r"депозит|залог"),
    "step_free": ("вход без ступеней", r"ступен|пандус|порог|вход"),
    "clear_width_mm": ("ширина прохода на входе, мм", r"ширин|проход|двер"),
    "threshold_mm": ("высота порога, мм", r"порог"),
    "has_lift": ("лифт", r"лифт"),
    "door_width_mm": ("ширина двери лифта, мм", r"двер"),
    "cabin_width_mm": (
        "ширина кабины, мм",
        r"ширин\w*\s+кабин|кабин\w*\s+(?:шириной|в\s+ширину)|шириной",
    ),
    "cabin_depth_mm": ("глубина кабины, мм", r"глубин"),
    "served_floors": ("на какие этажи ходит", r"этаж"),
    "step_free_shower": ("душ без бортика", r"душ|бортик|поддон"),
    "shower_threshold_mm": ("высота бортика душа, мм", r"бортик|порог|поддон"),
    "min_door_width_mm": ("минимальная ширина дверей в номер и санузел, мм", r"двер|ширин"),
    "download_mbps": ("скорость загрузки, Мбит/с", r"загрузк|скачиван|download|входящ"),
    "upload_mbps": ("скорость отдачи, Мбит/с", r"отдач|upload|исходящ|выгрузк"),
    "latency_ms": ("задержка, мс", r"задержк|пинг|ping|latency"),
    "jitter_ms": ("джиттер, мс", r"джиттер|jitter"),
    "loss_percent": ("потери пакетов, %", r"потер"),
    "has_desk": ("рабочий стол", r"стол"),
    "desk_width_mm": ("ширина стола, мм", r"ширин|стол"),
    "socket_near_desk": ("розетка у стола", r"розетк"),
    "bed_configuration": ("кровати", r"кроват|твин|двуспальн|раздельн"),
    "private_balcony": ("балкон", r"балкон|лоджи"),
    "private_terrace": ("терраса", r"террас"),
    "area_m2": ("площадь, м²", r"м2|м²|кв|площад"),
    "private_kitchenette": ("кухня в номере", r"кухн|плит"),
    "cooking_allowed": ("можно готовить", r"готов"),
    "in_room_kettle": ("чайник в номере", r"чайник"),
    "on_request": ("по запросу", r"по\s+запрос|по\s+просьб"),
    "has_safe": ("сейф", r"сейф"),
    "safe_inner_width_mm": ("ширина сейфа внутри, мм", r"сейф|ширин"),
    "mandatory_fees_total": ("обязательные доплаты, ₽", r"доплат|сбор|налог|руб|₽"),
}

# Правдоподобные области по единицам: вне — значение отклоняется, а не исправляется.
DOMAINS: dict[str, tuple[float, float]] = {
    "kg": (0, 100),
    "mm": (0, 5000),
    "Mbit/s": (0, 10000),
    "ms": (0, 5000),
    "%": (0, 100),
    "RUB": (0, 1_000_000),
    "kW": (0, 400),
    "years": (0, 18),
    "m": (0, 10),
    "m2": (0, 500),
    "dB(A)": (10, 120),
}

# Предзаполнение v9 (атом заявления Островка) → поле того же смысла. Только они дают противоречие.
CLAIM_FIELDS: dict[tuple[str, str], str] = {
    ("A03", "lift_presence"): "has_lift",
    ("F03", "cot_available"): "cot_available",
    ("L04", "reception_24h"): "front_desk_24h",
    ("L04", "prior_arrangement"): "prior_arrangement",
    ("M02", "balcony_presence"): "private_balcony",
    ("P01", "pets_allowed"): "pets_allowed",
    ("S04", "cooling_presence"): "has_ac",
}
# Метки оснащения номера в предзаполнении (`published_room_equipment_tag`) того же смысла.
CLAIM_TAGS: dict[tuple[str, str], str] = {("M05", "safe"): "has_safe", ("R03", "desk"): "has_desk"}

AnswerState = Literal["answered", "unknown", "varies", "refused", "contradictory", "not_applicable"]


def label(field: str) -> str:
    return FIELDS.get(field, (field, ""))[0]


def keywords(field: str) -> str:
    return FIELDS.get(field, ("", field))[1]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldAnswer(_Strict):
    name: str
    value: str
    unit: str
    quote: str


class AnswerCompact(_Strict):
    """Рабочая схема для модели: всё обязательно, значения строками, цитата у каждого поля."""

    answer_state: AnswerState
    quantifier: Literal["all", "some", "none", "unknown"]
    fields: list[FieldAnswer]
    conditions: str
    unclear: list[str]


def fields_listing(feature: Feature) -> str:
    lines = []
    for spec in feature.fields.values():
        extra = f" [{spec.unit}]" if spec.unit else ""
        if spec.values:
            extra += f" значения: {', '.join(spec.values)}"
        lines.append(f"- {spec.name} ({spec.type}{extra}) — {label(spec.name)}")
    return "\n".join(lines)


def answer_schema() -> dict[str, Any]:
    return AnswerCompact.model_json_schema()
