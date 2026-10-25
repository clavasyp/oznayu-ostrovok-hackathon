"""Почему «неизвестно» — человеческим языком для гостя и админа.

У факта и у результата проверки условия причина хранится машинным кодом (`unknown_reason`,
`reason_code`): по нему считаются покрытие и очередь работ. Здесь — короткий текст для экрана.
Коды v9 бывают двух видов: короткие (`no_building_match`) и длинные английские фразы —
для них ключ по началу фразы.

Новый код без текста не ломает ответ: показывается «Пока нет данных», код остаётся рядом.
"""

from __future__ import annotations

# Причины движка поиска (core/search.py).
ENGINE: dict[str, str] = {
    "no_fact": "Об этом у нас пока нет данных",
    "estimate_only": "Есть только оценка — для обязательного условия её мало",
    "not_applicable": "К этому отелю не относится",
    "conflict": "Источники расходятся — уточним у отеля",
    "depends_on_building": "Зависит от корпуса — уточним у отеля, где будет номер",
    "depends_on_room": "Зависит от номера: часть номеров категории подходит — уточним у отеля",
    "not_comparable": "Значение не сравнить с условием",
    "out_of_catalog": "Такое мы пока не знаем",
    "no_placements": "Не знаем, на каких этажах и сторонах номера этой категории — спросим отель",
    "placements_incomplete": "Отель описал не все номера категории — уточним остальные",
    "not_guaranteed": "Отель не гарантирует номер с нужным этажом или стороной",
    "no_window_fact": "Для этого окна расчёта пока нет",
    "no_room_types": "Категории номеров этого отеля ещё не загружены",
    "no_single_placement": "По отдельности условия выполняются, но ни в одном номере — все сразу",
    "unknown": "Пока нет данных",
}

# Короткие коды v9.
CODES: dict[str, str] = {
    "no_building_match": "Не нашли здание отеля на карте — этажность, вид и солнце не посчитать",
    "no_verified_window_height_ground_datum_and_complete_obstacles": (
        "Нет высот окон и соседних зданий — вид и солнце пока не посчитать"
    ),
    "no_verified_existing_window_height_and_complete_scene": (
        "Нет высоты окна и соседних зданий — вид и солнце пока не посчитать"
    ),
    "no_confirmed_open_service_hours_in_cached_source_or_extent": (
        "Часы работы мест рядом не подтверждены"
    ),
    "verified_hotel_building_footprint_missing_or_ambiguous": "Контур здания отеля не подтверждён",
    (
        "verified_hotel_building_footprint_rejected_or_unconfirmed_"
        "by_strict_full_address_corpus_gate"
    ): "Здание отеля не подтверждено по полному адресу",
    "no_verified_construction_completion_event": "Год постройки не подтверждён",
    "no_current_official_heritage_registry_match": "В реестре объектов наследия отель не найден",
    "no_connected_eligible_candidate_in_cached_graph": (
        "Подходящее место с пешим путём по карте не найдено — это не значит, что его нет"
    ),
    "no_connected_path_in_cached_graph_not_proof_no_real_path": (
        "Пеший путь по карте не построен — это не значит, что его нет"
    ),
    "missing_aboveground_levels": "Этажность здания неизвестна",
    "hotelier_does_not_know": "Отель не знает ответа",
    "multiple_buildings_or_counting_scope_conflict": (
        "У отеля несколько зданий — этажность неоднозначна"
    ),
    "hotel_snap_over_100m": "Отель дальше 100 м от пешеходной сети на карте — путь не посчитать",
    "hotel_or_target_snap_over_100m": (
        "Отель или цель дальше 100 м от пешеходной сети на карте — путь не посчитать"
    ),
    "undefined_lighting_fraction_zero_graph_length": (
        "Освещённость пути не посчитать: путь не найден"
    ),
}

# Длинные фразы v9: ключ — начало фразы.
PREFIXES: list[tuple[str, str]] = [
    (
        "No licensed complete current operator schedule",
        "Нет актуального расписания транспорта на ваши даты",
    ),
    (
        "No accepted dated, geocoded primary-source event",
        "Нет надёжных данных о стройках и перекрытиях на ваши даты",
    ),
    (
        "No OSM/hotelier-confirmed target entrance and public-admission/animal",
        "Не подтверждено, пускают ли на площадку с собакой",
    ),
    (
        "No zero-gap surveyed constraints",
        "Безбарьерность пути не проверена: бордюры, ширина, уклон, лестницы, лифты",
    ),
    (
        "Requested trips beyond16day forecast",
        "Прогноз погоды на эти даты ещё не вышел",
    ),
    (
        "Requested trips beyondOpenMeteoairquality",
        "Прогноз качества воздуха на эти даты ещё не вышел",
    ),
    (
        "D22 selects Open-Meteo asprimaryairsource",
        "Есть только модельный расчёт качества воздуха, измерений станций нет",
    ),
    (
        "No corrected BlackMarble",
        "Нет проверенных данных о засветке ночного неба",
    ),
    (
        "No licensed georeferenced accepted facade-height Lnight",
        "Нет проверенной карты ночного шума у окон",
    ),
    (
        "No matched police-category numerator",
        "Нет официальной статистики преступлений по району",
    ),
    (
        "No complete official geocoded injury-crash",
        "Нет полной официальной статистики ДТП на маршруте",
    ),
    (
        "No licensed fresh OpenCellID",
        "Нет свежих данных о вышках мобильной связи",
    ),
    (
        "No dated official material descriptor",
        "Нет официального описания покрытия пляжа",
    ),
    (
        "Both requested trips beyond the16day marine",
        "Прогноз моря на эти даты ещё не вышел",
    ),
    (
        "No matched authorized latest beach flag",
        "Нет официального статуса пляжа",
    ),
    (
        "No published authorized labprotocol",
        "Нет лабораторных проб воды у этого пляжа",
    ),
    (
        "No selected connected route in cached graph",
        "Маршрут по карте не построен — это не значит, что пути нет",
    ),
    (
        "No eligible target/connected route in cached graph",
        "Площадка рядом на карте не найдена — это не значит, что её нет",
    ),
    (
        "Ground DTM/survey absent",
        "Нет рельефа — подъём по пути не посчитать",
    ),
]

FALLBACK = "Пока нет данных"


def reason_text(code: str | None) -> str:
    """Короткий текст для экрана по коду причины."""
    if not code:
        return FALLBACK
    if code in ENGINE:
        return ENGINE[code]
    if code in CODES:
        return CODES[code]
    for prefix, text in PREFIXES:
        if code.startswith(prefix):
            return text
    return FALLBACK


def is_known(code: str) -> bool:
    return reason_text(code) != FALLBACK or code == "unknown"
