# Разбор отчёта v4a «Алгоритмы и библиотеки калькуляторов»

- **Отчёт:** `reports/Алгоритмы и библиотеки калькуляторов.md`.
- **Сверка:** подзадача Claude, 23.10.2026. Проверено 15 утверждений: 14 подтвердились, одно частично неверно.
- **Итог:** отчёт сильный. Слабые места: устаревший взгляд на D24, нет оценок на реальный город (только игрушечные замеры), нет итоговых размеров образов.

## Выбор по группам калькуляторов

| Группа | Выбор (лицензия) | Главные подводные камни |
|---|---|---|
| Фасады | Shapely 2.2 + GeoPandas 1.2 (BSD-3), pyproj (MIT) в сборке; PostGIS — хранение | `building:part` режем на ячейки с интервалами высот. Общая стена исключается только в общем диапазоне высот. Высота «этажи × 3 м» — только `estimate` (D18) |
| Отель → здание | STRtree / `sjoin` + PostGIS `ST_Covers` / `ST_DWithin`; связь многие-ко-многим | Ближайшее здание — только кандидат. Адрес или ID сильнее расстояния. `no_building_match` остаётся в знаменателе |
| Вид и горизонт | Свои 2,5D-лучи: Shapely STRtree по зданиям на 500–1000 м + рельеф дальше. GRASS `r.viewshed` (GPL) — только независимый эталон | Разные соглашения об азимутах (GRASS: восток = 0 против часовой; PVGIS: север = 0 по часовой). NoData не считать прозрачным. Кривизну не учитывать дважды |
| Солнце | pvlib 0.16.1 (`spa_python`, BSD-3); astral — перекрёстная проверка | Только IANA-часовые пояса. Деревья — сценарии с листвой и без |
| Пешие маршруты | OSMnx 2.1 (MIT) + NetworkX (BSD-3); позже сравнить Valhalla (MIT). **Pandana — AGPL, не брать** | «Без ступеней» — запрет ребра, а не штраф. Матрица 1000×N — обратный multi-source Dijkstra. **OSMnx не читает PBF:** нужен pyosmium (BSD-2) или pyrosm (MIT), либо граф из таблицы PostGIS |
| `opening_hours` | opening-hours-py 2.1.4 (MIT / Apache-2.0) + holidays | **Праздников ОАЭ нет:** `PH` в Дубае → `unknown`. Доля нераспарсиваемых значений не измерена |
| GTFS | OTP 2.10 (LGPL) — маршруты; r5py (GPL / MIT, JDK 21) — матрицы; gtfs-validator (Apache-2.0) | Время 25:35 допустимо; `inactive` — риск устаревания фида, а не отмена транспорта |
| Шум (`road_exposure`) | Свой `noise_proxy@1`: факторы и порядковая шкала без дБ. NoiseModelling (GPL-3) — только при данных о трафике | Отсутствие слоя ≠ ноль объектов. Калибровка по NoiseCapture — с пространственным разбиением выборки |
| Дедупликация | Правила + RapidFuzz (MIT) + ревью; позже Splink 5 (MIT). **Unidecode — GPL:** вместо него anyascii (ISC) или PyICU | Турецкие I / İ / ı; арабский ↔ латиница. Из A≈B и B≈C не следует A=C |
| Среда и климат | Open-Meteo по HTTP (httpx) в Dagster, сырой JSON в объектном хранилище; WorldCover и Black Marble — через Rasterio | **Historical / Climate API — только с плана Professional.** Модель задаём явно (`era5_land` для температуры, `era5` для осадков), без `best_match`. Климатическая норма 1991–2020. Температура моря с 2022 года → `estimate`. Пыльца только для Европы |
| Упаковка | GeoParquet (PyArrow), DuckDB spatial (MIT), rio-cogeo, tippecanoe (BSD-2), `pg_dump -Fc` / `pg_restore` | `pg_restore -j` несовместим с `--single-transaction`, поэтому staging с последующей подменой |

## Образы и порядок реализации

**Образы — targets одного Dockerfile:**
- **api:** FastAPI, psycopg, Alembic, opening-hours-py, tzdata. Без GDAL и JDK.
- **builder (Dagster):** shapely, geopandas, pyproj, pyogrio, rasterio, rio-cogeo, pvlib, osmnx + pyosmium, networkx, rapidfuzz, holidays, gtfs-kit, pyarrow, duckdb, httpx.
- **тяжёлые профили:** GRASS, Valhalla, OTP, r5py + JDK, planetiler, NoiseModelling.

**В образы не ставим:** pandana (AGPL), Unidecode (GPL), pyopening-hours (GPL, заброшен), OptiX; cdsapi и copernicusmarine (после D22 — только для контроля).

**Порядок реализации:**
1. Каркас D16.
2. Каталог и привязка отель → здание.
3. Простые признаки: шум-факторы, зелень, засветка, Open-Meteo.
4. Маршруты и часы работы.
5. Фасады и высоты.
6. Вид и солнце.
7. GTFS.
8. Splink и NoiseModelling.
9. Масштаб и холодный старт ≤ 600 с.

## Сверка (ключевое)

- ✅ **MinIO: репозиторий архивирован 25.04.2026,** «THIS REPOSITORY IS NO LONGER MAINTAINED». Community-версия распространяется только исходниками, без готовых бинарников и образов. AGPLv3. **Перепроверено Claude по github.com/minio/minio.**
- ✅ **Open-Meteo:** коммерческое использование — с плана Standard, но Historical / Climate / Ensemble — **только с Professional**. Бесплатный API — некоммерческий; лимиты 600/мин, 5000/ч, 10 000/сут, 300 тыс./мес.
- ✅ **Лицензии:** pandana AGPL; r5py GPL / MIT; OTP LGPL; Unidecode GPL; Valhalla MIT (проверка `max_grade` у пешеходов закомментирована); copernicusmarine EUPL; NoiseModelling GPL-3.
- ✅ **opening-hours-py:** праздники из Nager.Date, ОАЭ нет.
- ⚠ **`ST_CoverageSimplify`** есть с PostGIS 3.4.0, но требует GEOS ≥ 3.12. Проверить GEOS в нашем образе PostGIS.

## Что меняем в проекте

1. **D16 / MinIO** — риск. Нужно решение пользователя:
   - закрепить последний опубликованный образ MinIO по digest;
   - собирать MinIO из исходников;
   - заменить на поддерживаемое S3-совместимое хранилище (например, SeaweedFS — Apache-2.0; проверить при выборе).

   В любом случае работаем через S3-адаптер, чтобы хранилище менялось конфигурацией.
2. **D22:** для климатических норм (C01–C03) нужен Open-Meteo **Professional** или свой сервер; для моря, воздуха и прогнозов хватает Standard. Модель задаётся явно на каждый признак.
3. **core:**
   - образы — targets api / builder / heavy;
   - OSM PBF читаем через pyosmium (или граф из PostGIS);
   - **query-artifacts:** профили горизонтов (~110 MiB на 1000 отелей, uint8 ~29 MB) в рабочем пакете — или только помесячные предрасчитанные значения;
   - в §1 описать состав D16: db, app, Dagster (webserver, daemon, code location), S3-хранилище, init.
4. **D24 в отчёте устарел. Правильно:**
   - `mid` Островка — основной ID;
   - привязка к зданию по координатам и адресу Островка;
   - `floorsNumber` — вход для высоты здания (`ostrovok_content`), при расхождении с OSM → `conflict`;
   - `facts.register` — ключ блокировки при дедупликации;
   - POI Островка с расстояниями — отдельный признак, а не рецепт `walk_to_beach` (D15).
