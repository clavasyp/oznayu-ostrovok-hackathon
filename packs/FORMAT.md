# Формат пакета города (версия 1)

Пакет — готовые результаты по городу. Собирается заранее, вне запуска жюри; при старте сервис `init` проверяет хеши и грузит пакет в базу (D6, core.md §9). Код — `core/packs.py`, загрузка — `pipeline/load_packs.py`.

```
packs/<city_id>/
  manifest.json
  data/
    hotels.jsonl.gz             отели: id (masterId), city_id, name, address, kind, stars, subzone, subzone_id, latitude, longitude, source
    room_types.jsonl.gz         категории: id = "<hotel_id>:<rgHash>", hotel_id, rg_hash, name, amenities, source
    placement_sets.jsonl.gz     размещения категорий (D17): room_type_id, completeness, guarantee, status, source
    placements.jsonl.gz         размещение: id ("<room_type_id>|<здание>|<этаж>[|<вариант>]"), room_type_id, building_id, floor, floor_label
    placement_windows.jsonl.gz  окна размещения: placement_id, facade_id, azimuth_deg
    buildings.jsonl.gz          корпуса: id ("way/…"), city_id, geometry (GeoJSON), tags
    hotel_buildings.jsonl.gz    связи: hotel_id, building_id, method (auto | auto_point | curated), evidence
    facades.jsonl.gz            building_id, facade_id, azimuth_deg, side, is_courtyard, geometry, length_m, source
    places.jsonl.gz             id (osm:<type>/<id> или 2gis:<id>), city_id, name, latitude, longitude, categories, tags, source
    transit_stations.jsonl.gz   id, city_id, name, latitude, longitude, mode, source
    hotel_stations.jsonl.gz     hotel_id, station_id, rank, distance_m, distance_kind, walking_distance_m, source
    transit_times.jsonl.gz      id, city_id, source_station_id, target_station_id, departure_at, profile, duration_s, transfers, source
    curation_items.jsonl.gz     кандидаты D33, схема core.db.CurationItem; только proposed
    facts.jsonl.gz              факты — ровно в формате core/facts.py (как факты v9)
    questions.jsonl.gz          вопросы отельеру с предзаполнением: id, hotel_id, feature_id, text, prefill, fields, state
    coverage.jsonl.gz           покрытие по характеристикам: feature_id, n_hotels, computed, estimate, unknown, not_applicable
    ota_comparisons.jsonl.gz    сверка с карточкой Островка: hotel_id, feature_id, ota_value, classification, source
    view_sun.jsonl.gz           слой «вид и солнце» (Сочи): факты V05 и V01 по окнам — kind "facts" (D40)
    hotel_amenities.jsonl.gz    слой «удобства отеля» (Москва, Сочи): A03, T04, L04, T05 и `onsite` — kind "facts" (D26)
    environment.jsonl.gz        слой «среда» (Москва, Сочи): климат C01–C03 и зелень E03 на все отели — kind "facts"
    sun_hours.jsonl.gz          слой «часы солнца» (Сочи): S01, S02 — верхняя граница по фасадам — kind "facts"
    mountains.jsonl.gz          слой «вид на горы» (Сочи): V02 по фасадам — kind "facts"
    building_years.jsonl.gz     слой «год постройки по OSM» (Москва, Сочи): H01 оценкой — kind "facts"
    heritage.jsonl.gz           слой «охранный статус» (Москва, Сочи): H02 — объект наследия в контуре корпуса — kind "facts"
    beach_surface.jsonl.gz      слой «покрытие пляжа» (Сочи): B01 оценкой — тег `surface` ближайшего пляжа W01 — kind "facts"
    construction.jsonl.gz       слой «стройка рядом» (Москва, Сочи): X03 оценкой — до края стройки OSM, «не ближе 1 км» — kind "facts"
    building_facts.jsonl.gz, buildings_auto_point.jsonl.gz, facades_auto_point.jsonl.gz,
    hotel_buildings_auto_point.jsonl.gz — связи отеля со зданием по точке (D42-1), H03, N02, N03
    buildings_auto_point.jsonl.gz        слой связи по точке (D42-1): новые корпуса — kind "buildings"
    facades_auto_point.jsonl.gz          их фасады `seg-…` — kind "facades"
    hotel_buildings_auto_point.jsonl.gz  связи `auto_point` — kind "hotel_buildings"
    building_facts.jsonl.gz              H03, N02, N03 корпусов и оценки N02 / N03 от точки — kind "facts"
```

**Правила:**
- Строка JSONL = одна запись, UTF-8, gzip.
- Факт проходит валидацию `core.facts.Fact`: у `unknown` есть `unknown_reason`, у статуса со значением есть `value`.
- **Окно** — субъект фактов вида, солнца и шума у окна: `subject_level = facade_physical_floor`, `subject_id = "<building_id>:<facade_id>:floor<N>"`, где `facade_id` — id отрезка контура из `facades.jsonl.gz` пакета (`seg-…`, см. «Дополнение D34/D39» ниже). Размещение категории ссылается на те же здание, фасад и этаж — так движок поиска находит факты окна (D17).
- **Категория** — субъект фактов уровня номера (кровати, балкон, Wi-Fi): `subject_level = room_type`, `subject_id = "<hotel_id>:<rgHash>"`.
- Новая версия пакета заменяет данные города целиком; факты, созданные в работе, остаются (`pipeline/load_packs.py`).
- В пакет идут только результаты и минимальная геометрия для карты. Сырые PBF, растры, графы и кеши API — нет.
- Источники с запретом распространения в пакет не попадают. Для отдельного хакатонного
  слоя 2ГИС действует D37: `commercial_ok=false` в `manifest.json`.

### Дополнение D34/D39 (ветка Севы)

Загрузчик поддерживает эти новые виды файлов и прежние пакеты v9; миграции — `0004`–`0006`.
Большие таблицы делятся по 250 000 строк, несколько файлов одного `kind` суммируются.
Строки загружаются потоковым COPY в общей транзакции города (D41: холодный старт ≤ 10 минут).
Ошибка откатывает также удаление прежних строк. Число строк сверяется с манифестом.

- **Подзона Сочи:** `subzone` — название для гостя («Адлер», «Красная Поляна», «Сочи (центр)»;
  вне именованных подзон — «Большой Сочи»), `subzone_id` — код (`adler`, `krasnaya_polyana`,
  `center`, `other_greater_sochi`) для фильтров.
- **Район Москвы:** `subzone` — район или поселение по границе OSM (admin_level 8; «Хамовники»,
  «Басманный», «Сосенское»), `subzone_id` — код латиницей (`khamovniki`); в `source.district` —
  отношение OSM и округ (`ЦАО`). Вне Москвы (у аэропортов в области) оба поля пустые. Пишет
  `pipeline/build_districts.py` на месте, как `refresh_places`. У остальных городов поля пустые.

- `facade_id = seg-<хеш концов сегмента и типа кольца>`, локальный в здании. Перенумерация
  обхода кольца ID не меняет; изменение самой геометрии может изменить ID. Субъект факта
  фасада — `<building_id>:<facade_id>`, окна — ещё `:floor<N>`.
- `is_courtyard=true` означает сторону внутреннего кольца нанесённого контура.
  Для внешнего кольца — `null`: открытый двор не исключён. Сам контур не доказывает окна.
- Имена OSM лежат в `buildings.tags.placement_catalog.mapped_names`. Они не становятся
  именами корпуса, употребляемыми отелем. Подтверждённые названия —
  `hotel_buildings.evidence.hotel_aliases`: `{name, status, source}`. Адаптер
  `core.placement_catalog.placement_maps` отдаёт только подтверждённые однозначные алиасы
  и направления. Сторона из ответа отеля — все отрезки корпуса на эту сторону длиной от 3 м
  (`side_segments`, D41-3): номер на одном из них, на каждый отрезок — своё размещение.
- Размещения из ответов в работе (`placement_set.source` без `pack`) и названия корпусов со слов
  отеля новая версия пакета не стирает: они возвращаются, если категория, корпус и все отрезки
  окон ещё есть (`placements_kept` / `placements_dropped` в итоге загрузки). У набора из пакета
  загрузчик пишет версию в `source.pack`.
- D34 `places` — **все нанесённые места с категорией** в 1500 м хотя бы от одного отеля, без
  ограничения top-k. Категория — id словаря мест (`agents/catalog/places.json`) или
  характеристики каталога, которой место нужно (`core.geo_store.CATALOG_PLACES`: аптека L01,
  еда L02, супермаркет L03, детская площадка F01, бар N04, выгул P03, ветклиника P04, коворкинг
  R04). Скамейки, урны, парковки, сами отели без категории в пакет не идут (D41).
- **Парк** (`park`: `leisure=park`; `leisure=garden` с названием; без `access=private|no|
  customers|permit|military`; парк без названия меньше 0,5 га — зелень двора, не парк) —
  протяжённое место. Расстояние до точки внутри километрового парка обманывает: край может быть
  через дорогу. Поэтому у парка в `places` — строка объекта (`osm:way/123`, точка внутри
  контура, все категории) и **точки края** `osm:way/123#edge0001…`: каждое кольцо контура
  (внешнее и внутренние) делится на равные отрезки не длиннее 150 м, в пакете — точки в 1500 м
  от отелей, категория у них только `park`. Паспорт — `source.geometry`: `method`
  (`edge-points.1`), `role` (`representative_point` | `edge`), `object` (id парка),
  `spacing_m`, `edge_length_m`, `area_m2`, `edge_points_near_hotels`. Ближайшая к отелю точка
  парка — **до края парка по прямой**: не меньше настоящего расстояния до края и больше его не
  более чем на 75 м (половина шага); отель внутри большого парка получает расстояние до
  ближайшей точки края или середины, а не 0. `GeoStore.nearest` / `nearby` отдают у такой точки
  `distance_kind=geodesic_to_edge_point`; в списке мест отеля парк — одной строкой (ближайшая
  его точка), `total` считает места, а не точки.
- **Граница страны.** Срез Сочи захватывает Абхазию. Места и станции вне полигона субъектов РФ
  из того же PBF (`region.geojson` среза: Краснодарский край relation/108082 и Адыгея
  relation/253256) в пакет не идут — ближайшей станцией не будет «Ԥсоу». В срезе есть
  `aerialway` и `attraction`: станции подъёмников (`ski_lift`) и аттракционы (`theme_park`).
- Места и станции пересобираются без полной сборки: `python -m connectors.osm_pbf --places-only`
  (срез только мест и станций) → `python -m pipeline.refresh_places` (таблицы `places`,
  `transit_stations`, `hotel_stations`, `transit_times`, манифест; источник `d34_places_osm`).
- **Пары «отель → место» в пакете не хранятся** (D41: 17 млн строк, 84% — уличная мебель).
  Их считает запрос PostGIS (`GeoStore.nearby`, `/api/hotels/{id}/nearby-places`): `ST_DWithin`
  по индексу GiST на `place.geog` и `ST_Distance` по сфероиду WGS84 от точки отеля — то же
  расстояние, что считала сборка (сверено с прежними парами: Δ ≤ 0,001 м). Корпуса самого отеля
  (`hotel_buildings`) в его выдачу не попадают. `distance_kind=geodesic_to_representative_point`:
  расстояние по прямой (у точки края парка — `geodesic_to_edge_point`, см. выше).
  `walking_distance_m=null` — маршрут/входы не проверены. Паспорт пары
  ссылается на `place.source` через `passport_ref`.
- **Характеристики каталога «место рядом»** (выгул P03, коворкинг R04, детская неотложка F02,
  аптека L01, еда L02, супермаркет L03, ветклиника P04) поиск тоже считает на запросе
  (`core/place_features.py`, `GeoStore.nearest` с рангом места): метки места (`tags`) читаются
  как есть — `opening_hours` (у еды — `opening_hours:kitchen`), `access`, `emergency`,
  `healthcare:speciality`, название. F02 ищется среди больниц словаря (`hospital`): неотложка
  подтверждена `emergency=yes` и педиатрией или названием; детская больница без отметки —
  «уточнить». Пересборка пакета для этого не нужна: метки в `places` хранятся целиком.
- Станции — ближайшие три объекта каждого нанесённого вида транспорта в 30 км по прямой;
  группировка объектов в пересадочный узел не выдумывается. Ранг относится к своему виду.
- `transit_times` хранит конкретное отправление **с часовым поясом** и профиль. Пара на
  другую дату/профиль возвращает `unknown`, без интерполяции и выдуманных пересадок.
  Пустая таблица поставляется явно и не означает отсутствие транспорта.
- `curation_items` — предложения, не факты. Повтор того же ID не перезаписывает решение
  и время проверки человека; при замене пакета журнал проверки сохраняется.
- 2ГИС хранится файлами с суффиксом `_2gis` при прежнем `kind`, с датированным паспортом
  и `commercial_ok=false` в источнике манифеста (D37, хакатон). Отзывы/фото/ключи запрещены
  и проверяются при присоединении bundle. Без ключа живые данные 2ГИС не заявляются.
- Минимальная карта — точки отелей/мест/станций, принятые контуры зданий и сегменты фасадов.
  Исходники, графы и подробные трассы маршрутов остаются вне пакета.

**`manifest.json`:**

```json
{
  "format_version": "1",
  "city_id": "sochi",
  "city_name": "Большой Сочи",
  "timezone": "Europe/Moscow",
  "depth": "full",
  "pack_version": "2026-10-26.1",
  "built_at": "2026-10-26T18:00:00+03:00",
  "code_version": "<git sha>",
  "sources": [{"id": "osm", "provider": "OpenStreetMap", "release": "2026-10-22", "license": "ODbL 1.0"}],
  "files": [{"path": "data/facts.jsonl.gz", "kind": "facts", "rows": 7148, "bytes": 3500000, "sha256": "…"}]
}
```

`depth`: `full` — Москва и Большой Сочи; `automatic` — 6 городов автоматическим слоем (D36).

**Слои калькуляторов.** Файл, который собирает не сборка v9, а отдельный калькулятор (сейчас — `python -m pipeline.build_view_sun`, `python -m pipeline.build_hotel_amenities --snapshot <снимок Островка>` и `python -m pipeline.build_building_links build --raw <сырьё OSM>`), добавляется в манифест через `core.packs.add_layer`:
- у файла в `files` — `"layer": true`; файлов одного `kind` может быть несколько (`facts.jsonl.gz` и `view_sun.jsonl.gz`), загрузка берёт все;
- у его источников — `"layer": "<путь файла>"`, id с префиксом слоя (`view_sun:copdem-glo30`);
- `pack_version` — версия сборки плюс хеш слоёв: `<версия сборки>+<хеш слоёв>` (например, `category-supplement.1-d55b41f99505+xxxxxxxx`);
- пересборка v9 слои сохраняет (`keep_layers`); слой без файла выпадает вместе с источниками.

**Связь по точке (D42-1, `pipeline/build_building_links.py`).** Отель без связи, чья точка внутри
ровно одного контура здания OSM, а адрес страницы не противоречит адресу здания (улица или дом
названы у обоих и расходятся — противоречие; не названо у одного — нет), получает связь
`method = "auto_point"` с доказательством в `evidence` (`rule`, `address_check`, версия объекта
OSM). Точка в нескольких контурах или ни в одном — связи нет. Прежние связи не меняются. Новые
корпуса и их фасады — в своих файлах-слоях, загрузчик берёт все файлы вида. Факты слоя
(`building-links.1`): H03 из `building:levels` (как v9, `observed`; тега нет — «неизвестно»
`missing_aboveground_levels`) для каждой связи, где его нет; N02 / N03 от контура (`N02-R2`,
`N03-R2`, параметры как у `route-poi.1`) для связей без них; отелю без корпуса — **оценка** N02 /
N03 от точки (`N02-R2-point`, `hard_condition_eligible: false`, D18). Сырьё — ответы Overpass вне
репозитория (`fetch --raw <папка>`: у каждого файла url, запрос, время, `timestamp_osm_base`,
sha256); `build` без сети проверяет sha256 и без папки сырья не запускается.
