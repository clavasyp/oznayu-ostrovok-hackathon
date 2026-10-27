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
    hotel_buildings.jsonl.gz    связи: hotel_id, building_id, method (auto | curated), evidence
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
  `center`, `other_greater_sochi`) для фильтров. У остальных городов оба поля пустые.

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
- **Пары «отель → место» в пакете не хранятся** (D41: 17 млн строк, 84% — уличная мебель).
  Их считает запрос PostGIS (`GeoStore.nearby`, `/api/hotels/{id}/nearby-places`): `ST_DWithin`
  по индексу GiST на `place.geog` и `ST_Distance` по сфероиду WGS84 от точки отеля — то же
  расстояние, что считала сборка (сверено с прежними парами: Δ ≤ 0,001 м). Корпуса самого отеля
  (`hotel_buildings`) в его выдачу не попадают. `distance_kind=geodesic_to_representative_point`:
  расстояние по прямой. `walking_distance_m=null` — маршрут/входы не проверены. Паспорт пары
  ссылается на `place.source` через `passport_ref`.
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

**Слои калькуляторов.** Файл, который собирает не сборка v9, а отдельный калькулятор (сейчас — `python -m pipeline.build_view_sun`), добавляется в манифест через `core.packs.add_layer`:
- у файла в `files` — `"layer": true`; файлов одного `kind` может быть несколько (`facts.jsonl.gz` и `view_sun.jsonl.gz`), загрузка берёт все;
- у его источников — `"layer": "<путь файла>"`, id с префиксом слоя (`view_sun:copdem-glo30`);
- `pack_version` — версия сборки плюс хеш слоёв: `<версия сборки>+<хеш слоёв>` (например, `category-supplement.1-d55b41f99505+xxxxxxxx`);
- пересборка v9 слои сохраняет (`keep_layers`); слой без файла выпадает вместе с источниками.
