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
    "ostrovok_partial": (
        "На Островке это указано, но не как обещание для каждого номера — уточним у отеля"
    ),
    "not_hard_eligible": "Есть расчёт, но с оговоркой — для обязательного условия его мало",
    "not_applicable": "К этому отелю не относится",
    "conflict": "Источники расходятся — уточним у отеля",
    "depends_on_building": "Зависит от корпуса — уточним у отеля, где будет номер",
    "depends_on_room": "Зависит от номера: часть номеров категории подходит — уточним у отеля",
    "not_comparable": "Значение не сравнить с условием",
    "out_of_catalog": "Такое мы пока не знаем",
    "partial_match": "Проверяем только частично — для обязательного условия этого мало, уточним",
    "place_not_found": "В наших данных о местах рядом такого не нашли — данные не полные, уточним",
    "place_far_on_foot": "По прямой близко, но пешком может быть дальше — путь не считали",
    "place_no_data": "О таких местах в этом городе у нас пока нет данных",
    "place_on_site": "Есть ли такое в самом отеле — спросим отель",
    # Характеристики каталога «место рядом» с часами работы (core/place_features.py).
    "place_hours_unknown": "Рядом есть, но часы работы на карте не указаны — уточним",
    "place_hours_vary": "Рядом есть, но в это время открыто не каждый день — уточним дату",
    "place_kitchen_unknown": "Рядом открыто, но подают ли там еду в это время — не указано",
    "place_closed_at_moment": "Рядом есть, но по часам работы в нужное время закрыто — уточним",
    "place_emergency_unconfirmed": (
        "Рядом детская больница, но неотложный приём на карте не отмечен — уточним"
    ),
    "place_on_site_seasonal": "В отеле есть, но сезонное — работает ли в ваши даты, уточним",
    "sun_hours_upper_bound": (
        "Это верхняя оценка солнца на фасаде: соседние дома и деревья могут закрыть часть — уточним"
    ),
    "floor_by_building_levels": "Этаж — по этажности корпуса на карте; где номер, уточним у отеля",
    "transit_on_request": "Время на транспорте считаем по 2ГИС для первой страницы выдачи",
    "transit_no_key": "Время на транспорте считаем по 2ГИС — ключ не подключён",
    "forecast_out_of_range": "Прогноз на эти даты ещё не вышел — проверим ближе к поездке",
    "forecast_unavailable": "Прогноз сейчас недоступен — проверим ближе к дате",
    "forecast_cell_empty": "Для этого места прогноз модели пуст",
    "trip_estimate": "Это оценка по прямой — время на транспорте пока не считаем",
    "trip_distance_unknown": "Нет точных координат отеля — расстояние до цели не посчитать",
    "trip_target_not_found": "Такую цель среди мест города мы не нашли — уточните название",
    "count_lower_bound": "По картам меньше нужного, но отмечены не все места — уточним",
    "place_quality_unknown": "Рядом есть, но насколько хорошее — оценок и рейтингов у нас нет",
    "no_placements": "Не знаем, на каких этажах и сторонах номера этой категории — спросим отель",
    "placements_incomplete": "Отель описал не все номера категории — уточним остальные",
    "not_guaranteed": "Отель не гарантирует номер с нужным этажом или стороной",
    "no_window_fact": "Для этого окна расчёта пока нет",
    "no_room_types": "Категории номеров этого отеля ещё не загружены",
    "no_single_placement": "По отдельности условия выполняются, но ни в одном номере — все сразу",
    "value_unspecified": "Указано, что это есть, но не сказано, что именно — уточним у отеля",
    "logic_invalid": "Не разобрали, как связаны условия запроса — уточним",
    "no_window_known": (
        "По расчёту не подходит ни одно окно известных нам корпусов — уточним у отеля, нет ли "
        "других"
    ),
    "window_some": "Подходят не все окна — уточним у отеля, где будет номер",
    "window_all": "Подходят все окна известных нам корпусов — уточним у отеля, где будет номер",
    "no_fact_params": "На эти даты расчёта пока нет",
    "unknown": "Пока нет данных",
}

# Вид и солнце по окнам (calculators/view_sun.py, D40): что решает «неизвестно».
VIEW: dict[str, str] = {
    "view_obstacle_height": (
        "Зависит от точной высоты здания напротив — по карте известна только этажность"
    ),
    "view_obstacle_height_tag": (
        "Зависит от точной высоты здания напротив — на карте она указана приблизительно"
    ),
    "view_obstacle_satellite": (
        "Зависит от высоты здания напротив — есть только оценка по спутниковым снимкам"
    ),
    "view_obstacle_unmeasured": (
        "На пути здание без высоты и этажности на картах — проверим его этажность"
    ),
    "view_trees": "На пути деревья — их высота неизвестна",
    "view_landcover_missing": "Нет данных о деревьях и воде на этом участке",
    "view_terrain": "Зависит от рельефа: модель рельефа слишком грубая для этого окна",
    "view_terrain_beyond": "Дальше по лучу нет данных о рельефе",
    "view_low_floor": "Нижние этажи: деревья и ограды вдоль улиц на картах неполные",
    "view_sun_oblique": "Солнце садится под острым углом к стене — зависит от окна и балкона",
    "view_sun_partial": "Солнце видно только часть последних 30 минут до заката",
    "view_need_dates": "Зависит от дат поездки — укажите даты",
    "view_sea_blocked": "По расчёту море закрыто зданиями — проверим по панораме",
    "view_window_position": "Зависит от того, в каком месте фасада окно",
    "floor_count_unknown": "Неизвестна этажность здания отеля",
}

# Короткие коды v9.
CODES: dict[str, str] = {
    "ostrovok_field_unknown_or_missing": "На странице Островка об этом не сказано",
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
    "source_extent_or_matching_source_geometry_unavailable": (
        "Ближайшая дорога или путь может быть за краем выгрузки карты — расстояние не считаем"
    ),
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
    # Климат C01–C03 и море B02 (calculators/env_climate.py, env_marine.py)
    "hotel_coordinates_missing": "У отеля нет координат — климат и море не посчитать",
    "incomplete_authorized_1991_2020_month_series": (
        "В архиве погоды 1991–2020 не хватает дней — среднее не считаем, чтобы не занизить"
    ),
    "marine_targets_changed_since_mapping": (
        "Отбор пляжей изменился после расчёта — температуру моря пересчитаем"
    ),
    "hotel_not_in_marine_targets": "Для этого отеля пляж ещё не выбран",
    "selected_target_is_not_accepted_sea_beach": (
        "Выбранный пляж не подтверждён как морской — температуру моря не берём"
    ),
    "beach_not_in_marine_mapping": "Пляж не найден в карте морских ячеек",
    "marine_cell_not_eligible": "Морская ячейка у пляжа не прошла проверку",
    "worldcover_valid_area_below_99_percent": (
        "Покров вокруг отеля размечен меньше чем на 99% — долю зелени не считаем"
    ),
    "no_classified_land_in_buffer": "Вокруг отеля нет размеченной суши — долю зелени не считаем",
    "era5_cell_missing": "Погодной ячейки этого места нет в архиве — климат не посчитан",
    "landcover_classes_invalid": "В спецификации неверные классы покрова — долю не считаем",
    "landcover_radius_invalid": "Радиус в спецификации вне 100–2000 м — долю не считаем",
    # Доля покрова в установке без геобиблиотек (calculators/env_landcover.py, env_vegetation.py)
    "geo_libs_missing": "Покров по карте WorldCover в этой установке не считается — долю не знаем",
}

# Длинные фразы v9: ключ — начало фразы.
PREFIXES: list[tuple[str, str]] = [
    # Коды с параметром в конце (путь, город, год-месяц) — calculators/env*
    ("city_utc_offset_unknown", "Не знаем часовой пояс города — климат по месяцам не посчитать"),
    ("era5_cache_missing", "Нет архива погоды 1991–2020 — климат не посчитан"),
    ("marine_targets_missing", "Пляж для отеля не выбран — температуру моря не к чему привязать"),
    ("marine_mapping_missing", "Морская ячейка для выбранного пляжа не рассчитана"),
    ("incomplete_native_6hour_month", "Архив моря за этот месяц неполный — среднее не считаем"),
    ("no_cached_series", "Нет архивного ряда моря за этот месяц"),
    ("worldcover_tile_missing", "Нет тайла покрова WorldCover для этого места"),
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
    if code in VIEW:
        return VIEW[code]
    if code in CODES:
        return CODES[code]
    for prefix, text in PREFIXES:
        if code.startswith(prefix):
            return text
    return FALLBACK


def is_known(code: str) -> bool:
    return reason_text(code) != FALLBACK or code == "unknown"
