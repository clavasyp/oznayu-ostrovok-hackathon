"""Поля ответа отельера: подписи, ключевые слова, правила «есть / нет» и схема для модели.

Имена полей — ровно `parsed_fields` вопроса v9 (контракт `core/hotelier.py`: лишнее поле — 422).
Типы и единицы — в словаре `agents/catalog/features.json`: одно место для поиска и для разбора.
Здесь — то, что нужно только разбору: подпись поля, корни слов, по которым поле узнаётся в
свободном ответе, и как поле выражает «есть / нет».
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agents.catalog import Feature

# поле → (подпись, корни слов для поиска в ответе)
FIELDS: dict[str, tuple[str, str]] = {
    "hvac_type": ("кондиционер: тип", r"кондиционер|кондей|охлажд|сплит|центральн"),
    "individual_control": (
        "гость сам управляет кондиционером",
        r"пульт|сам\w*\s+(?:включ|регул|настра)|управл",
    ),
    "operating_season": ("в какой период работает", r"круглый год|сезон|с\s+\w+\s+по"),
    "restrictions": ("ограничения", r"ограничен|кроме"),
    "opaque_coverage": (
        "насколько шторы закрывают окна",
        r"затемн|blackout|блэкаут|темнот|штор|ставн",
    ),
    "edge_gaps": ("просветы по краям", r"просвет|по краям|щел"),
    "evidence_date": ("дата фото", r"фото"),
    "LAeq_dBA": ("измеренный уровень шума ночью, дБ(A)", r"дб|децибел"),
    "interval": ("интервал замера", r"интервал|с\s+\d+\s+до\s+\d+"),
    "measurement_protocol": ("как мерили", r"протокол|методик"),
    "windows_state": ("окна при замере", r"окн"),
    "hvac_state": ("кондиционер при замере", r"кондиционер"),
    "instrument": ("прибор", r"прибор|шумомер"),
    "shared_wall_with_lift": (
        "общая стена с лифтовой шахтой",
        r"лифтов\w+\s+шахт|стен\w+\s+с\s+лифт",
    ),
    "assignment_guarantee": ("можно гарантировать при брони", r"гарант|закреп"),
    "parking_owner": ("чья парковка", r"парковк|стоянк"),
    "reservation": ("нужно бронировать заранее", r"брон|заранее|резерв"),
    "vehicle_dimensions": ("габариты машины", r"габарит|высот\w*\s+(?:авто|машин)"),
    "fee": ("доплата, ₽", r"руб|₽|бесплатн|доплат|стоит"),
    "currency": ("валюта", r"руб|₽|евро|доллар"),
    "charging_basis": ("как берётся плата", r"сутк|за\s+вс[её]|бесплатн"),
    "access_hours": ("часы доступа", r"круглосуточн|часы"),
    "charger_id": ("зарядная станция", r"станци"),
    "connector": ("разъём зарядки", r"зарядк|зарядн|type\s*2|тайп|ccs|chademo|gb/?t|разъ[её]м"),
    "power_kW": ("мощность, кВт", r"квт|kw|мощн"),
    "working_at": ("когда работает", r"работает"),
    "external_access": ("доступна не только гостям", r"всем|посторонн|внешн"),
    "front_desk_hours": ("часы работы стойки регистрации", r"стойк|ресепшн|рецепц|регистрац"),
    "self_checkin": (
        "самозаселение",
        r"самозасел|по\s+коду|бесконтакт|ключ\w*\s+в\s+(?:ящик|сейф)",
    ),
    "prior_arrangement": ("нужно предупредить заранее", r"предупред|заранее|согласов"),
    "contact_deadline": ("до какого времени предупредить", r"предупред\w*\s+до"),
    "breakfast_start": ("начало завтрака", r"завтрак"),
    "breakfast_box": ("завтрак с собой", r"с\s+собой|ланч-?бокс|breakfast box"),
    "order_deadline": ("заказать до", r"заказ"),
    "child_age": ("до какого возраста ребёнка, лет", r"лет|возраст"),
    "weight_limit": ("предельная масса ребёнка, кг", r"кг"),
    "cot_dimensions": ("детская кроватка", r"кроватк"),
    "reservation_guarantee": ("можно гарантировать кроватку", r"гарант"),
    "internal_connecting_door": (
        "смежные номера с внутренней дверью",
        r"смежн|соедин|внутренн\w+\s+двер",
    ),
    "guaranteed_pair": ("можно гарантировать пару номеров", r"гарант"),
    "booking_method": ("как забронировать пару", r"брониров"),
    "pool_id": ("детский бассейн", r"бассейн"),
    "minimum_age": ("с какого возраста, лет", r"лет|возраст"),
    "depth_m": ("глубина бассейна, м", r"глубин"),
    "hours": ("часы работы", r"часы|с\s+\d"),
    "season": ("сезон", r"сезон|летн\w+\s+период"),
    "supervision_rules": ("присмотр", r"присмотр|присматр|инструктор|спасател"),
    "species": ("какие животные", r"живот|питом|собак|кошк|\bкот"),
    "weight_kg": ("максимальная масса животного, кг", r"кг|килограм|вес|масс"),
    "number": (
        "сколько животных",
        r"не\s+более\s+\d|до\s+\d+\s+(?:живот|питом|собак)|одн\w+\s+(?:живот|питом|собак)",
    ),
    "restricted_areas": ("куда с животным нельзя", r"нельзя\s+в|кроме|запрещ"),
    "prior_approval": ("согласовать заранее", r"согласов|заранее|предупред"),
    "fee_amount": ("доплата за животное, ₽", r"руб|₽|доплат|бесплатн"),
    "fee_basis": ("за сутки или за всё проживание", r"сутк|ноч|за\s+(?:вс[её]|период|проживан)"),
    "deposit": ("депозит, ₽", r"депозит|залог"),
    "refund_rules": ("возврат депозита", r"возвра"),
    "clear_width_mm": ("ширина прохода на входе, мм", r"ширин|проход"),
    "threshold_mm": ("высота порога на входе, мм", r"порог"),
    "route_to_lobby": ("путь от входа до лобби", r"пандус|ступен|лобби"),
    "lift_id": ("лифт", r"лифт"),
    "door_width_mm": ("ширина двери лифта, мм", r"двер"),
    "cabin_width_mm": (
        "ширина кабины лифта, мм",
        r"ширин\w*\s+кабин|кабин\w*\s+(?:шириной|в\s+ширину)",
    ),
    "cabin_depth_mm": ("глубина кабины лифта, мм", r"глубин"),
    "served_physical_floors": ("на какие этажи ходит", r"этаж"),
    "shower_threshold_mm": ("высота бортика душа, мм", r"бортик|порог|поддон"),
    "door_clear_widths_mm": ("минимальная ширина дверей в номер и санузел, мм", r"двер|ширин"),
    "bathroom_width_mm": ("ширина санузла, мм", r"санузл|ванн"),
    "download_Mbps": ("скорость загрузки, Мбит/с", r"загрузк|скачиван|download|входящ"),
    "upload_Mbps": ("скорость отдачи, Мбит/с", r"отдач|upload|исходящ|выгрузк"),
    "latency_ms": ("задержка, мс", r"задержк|пинг|ping|latency"),
    "jitter_ms": ("джиттер, мс", r"джиттер|jitter"),
    "loss_percent": ("потери пакетов, %", r"потер"),
    "desk_clear_width_mm": ("ширина рабочего стола, мм", r"стол"),
    "desk_depth_mm": ("глубина стола, мм", r"глубин"),
    "desk_height_mm": ("высота стола, мм", r"высот"),
    "socket_distance_mm": ("расстояние до розетки, мм", r"розетк"),
    "bed_configuration": ("кровати", r"кроват|твин|двуспальн|раздельн"),
    "bed_dimensions_mm": ("размер кроватей", r"\d+\s*[xх×]\s*\d+"),
    "split_possible": ("можно раздвинуть кровати", r"раздвин|разъедин"),
    "private_balcony": ("балкон", r"балкон|лоджи"),
    "private_terrace": ("терраса", r"террас"),
    "area_m2": ("площадь, м²", r"м2|м²|кв|площад"),
    "safety": ("ограждение", r"огражд|перил"),
    "private_kitchenette": ("кухня в номере", r"кухн|плит"),
    "appliances": ("техника на кухне", r"плит|холодильник|микроволнов|посуд"),
    "cooking_allowed": ("можно готовить", r"готов"),
    "in_room_kettle": ("чайник в номере", r"чайник"),
    "on_request": ("по запросу", r"по\s+запрос|по\s+просьб"),
    "guarantee": ("гарантируем", r"гарант"),
    "safe_inner_width_mm": ("ширина сейфа внутри, мм", r"ширин"),
    "safe_depth_mm": ("глубина сейфа, мм", r"глубин"),
    "safe_height_mm": ("высота сейфа, мм", r"высот"),
    "access": ("сейф", r"сейф"),
    "mandatory_fee_items": ("обязательные доплаты", r"доплат|сбор|налог"),
    "deposit_items": ("депозиты", r"депозит|залог"),
}

# «Есть / нет» через значение поля: (значение при «да», значение при «нет»). Значение «есть»
# ставится, только если отельер не назвал тип: «да, лифт есть» → lift_id = "present".
PRESENCE: dict[str, tuple[str, str]] = {
    "hvac_type": ("present", "none"),
    "connector": ("present", "none"),
    "cot_dimensions": ("present", "none"),
    "pool_id": ("present", "none"),
    "lift_id": ("present", "none"),
    "access": ("present", "none"),
    "parking_owner": ("own", "none"),
}
# Перечисления по словам ответа (порядок важен: первое совпадение).
ENUM_WORDS: dict[str, list[tuple[str, str]]] = {
    "hvac_type": [(r"сплит", "split"), (r"центральн", "central"), (r"фанкойл", "fan_coil")],
    "connector": [
        (r"type\s*2|тайп\s*2", "type2"),
        (r"ccs", "ccs"),
        (r"chademo", "chademo"),
        (r"gb/?t", "gbt"),
        (r"бытов\w+\s+розетк|schuko", "schuko"),
    ],  # fmt: skip
    "parking_owner": [
        (r"сторонн|городск|муниципальн", "third_party"),
        (r"на\s+улице|уличн", "street"),
    ],
    "opaque_coverage": [(r"не\s+полност|частичн|просвет", "partial"), (r"полност", "full")],
    "bed_configuration": [
        (r"двуспальн\w*\s+или\s+(?:раздельн|твин)", "double_or_twin"),
        (r"раздельн|твин", "twin"),
        (r"двуспальн", "double"),
    ],  # fmt: skip
    "windows_state": [(r"закрыт", "closed"), (r"открыт", "open")],
    "hvac_state": [
        (r"кондиционер\w*\s+(?:был\s+)?выключ", "off"),
        (r"кондиционер\w*\s+(?:был\s+)?включ", "on"),
    ],
    "fee_basis": [(r"сутк|ноч", "per_night"), (r"за\s+(?:вс[её]|период|проживан)", "per_stay")],
    "charging_basis": [
        (r"бесплатн", "free"),
        (r"сутк", "per_day"),
        (r"за\s+(?:вс[её]|период)", "per_stay"),
    ],
}
# Идентификаторы и служебные поля: из свободного текста правилами не заполняются.
CONTEXT_FIELDS = frozenset(
    {
        "room_type_id", "variant_ids", "room_ids", "window_ids", "room_type_ids", "charger_id",
        "entrance_id", "booking_id", "room_type_pair", "evidence_date", "measurement_date",
        "measurement_protocol", "instrument", "protocol", "time_samples", "endpoint", "dates",
        "service_date", "arrival_local", "occupancy", "currency",
    }
)  # fmt: skip

UNIT_RU = {
    "mm": "мм", "kg": "кг", "RUB": "₽", "Mbit/s": "Мбит/с", "ms": "мс", "m": "м", "m2": "м²",
    "years": "лет", "kW": "кВт", "%": "%", "dB(A)": "дБ(A)",
}  # fmt: skip
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

AnswerState = Literal["answered", "unknown", "varies", "refused", "contradictory", "not_applicable"]


def label(field: str) -> str:
    return FIELDS.get(field, (field, ""))[0]


def keywords(field: str) -> str:
    return FIELDS.get(field, ("", ""))[1] or field


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
        if spec.name in PRESENCE:
            yes, no = PRESENCE[spec.name]
            extra += f"; нет — «{no}», есть без уточнений — «{yes}»"
        lines.append(f"- {spec.name} ({spec.type}{extra}) — {label(spec.name)}")
    return "\n".join(lines)


def answer_schema() -> dict[str, Any]:
    return AnswerCompact.model_json_schema()
