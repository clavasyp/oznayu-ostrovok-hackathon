# Контракты (черновик лида, 24.10)

Договорённости о форматах, от которых зависят все. Код — источник правды, этот файл — карта.

| № | Контракт | Код | Кто опирается |
|---|---|---|---|
| 1 | **Факт с паспортом** | `core/facts.py` (обмен), `core/db.py` (таблицы), `migrations/` | все |
| 2 | **Интерфейс плагина** | `core/plugins.py`; пример — `calculators/env_vegetation.py` (E03) | Сева, Лера, лид |
| 3 | **Разобранный запрос (AST)** | `agents/query_ast.py`, `agents/schemas/query_ast.schema.json`, пример `agents/schemas/examples/track_query.json` | Данил, лид, Андрей |
| 4 | **API** | `app/api/routes.py` — пока на заглушках из фактов v9 (`app/fixtures/`) | Андрей, Данил |
| 5 | **Пакет города** | `core/packs.py`, `packs/FORMAT.md`, загрузка — `pipeline/load_packs.py`, `pipeline/init.py` | Сева, лид |

## 1. Факт

- **Поля — как в фактах v9** (`research_notes/Расчёт характеристик/integrated/*_facts.jsonl`): `fact_id`, `hotel_id`, `city_id`, `subject_level`, `subject_id`, `feature_id`, `params`, `value`, `unit`, `status`, `unknown_reason`, `sources`, `recipe_id`, `algorithm_version`, `confidence {level, reason}`, `observed_at`, `calculated_at`, `valid_to`, `scope_note`, `recipe_attempts`, `depends_on`. Лишние поля v9 сохраняются: в базе — в колонке `extra`.
- **ID — текстовые:** `masterId` Островка для отелей, `way/…` для корпусов OSM, 24 hex для фактов (`make_fact_id` — детерминированный).
- **Статусы:** `observed | computed | estimate | ostrovok_content | curated | hotelier | simulated_hotelier | unknown | not_applicable`.
- **Правила, которые проверяет модель:**
  - у `unknown` обязательна `unknown_reason`;
  - у статуса со значением обязательно `value`.
- **Обязательное условие** закрывают только `observed`, `computed`, `ostrovok_content`, `curated`, `hotelier`, `simulated_hotelier` — и только если `hard_condition_eligible` не `false`. Оценки — нет (D18). См. `Fact.usable_in_hard`.

## 2. Плагин

Калькулятор: `id`, `version` (→ `algorithm_version`), `feature_ids`, `run(subjects, ctx) -> list[Fact]`. Регистрация — декоратор `@register_calculator`. Неудача рецепта — факт `unknown` с причиной и `recipe_attempts`, а не исключение. Коннектор: `available(ctx)`, `fetch(ctx, workdir) -> list[DatasetRef]`.

## 3. AST

`hard` / `soft` (с `weight`) / `clarify` / **`out_of_catalog`** (D34: части запроса без характеристики не теряются). Условие: `feature_id` (ID каталога), `op`, `value`, `field` (какое поле значения сравнивать: `distance_m`, `count`, `mean`…), `params` (месяц, цель маршрута, время), `binding` (`same_placement` для уровня номера, D17). Компактную схему для модели (ограничения Claude на схемы) делает Данил.

## 4. API (заглушки)

| Метод | Что |
|---|---|
| `GET /health`, `/live`, `/ready` | 503 с фазой, пока идёт запуск; затем `{"status":"ok"}` |
| `GET /api/meta/labels` | Слова на экранах: статусы, «Забронировано», пометка имитации оформления |
| `GET /api/cities`, `/api/cities/{id}/coverage` | Города (глубина `full` / `automatic`), покрытие 54 характеристик |
| `GET /api/hotels?city_id=`, `/api/hotels/{id}`, `/api/facts/{fact_id}` | Отели показа, факты с паспортами |
| `POST /api/search` | `{text, city_id, ast}` → AST + результаты со статусами; без `ast` — пример запроса трека |
| `GET /api/hotelier/{id}/questions`, `POST …/answers` | 26 вопросов с предзаполнением; ответ пока не сохраняется |
| `GET /api/conflicts?city_id=` | Сравнения с карточкой Островка |

У ответов есть пометка `"stub": true`: данные настоящие, логика временная. Поиск-заглушка проверяет условия на уровне отеля; настоящий движок проверяет условия по размещениям категорий (D17).

## 5. Пакет города

См. `packs/FORMAT.md`. `init`:
1. миграции;
2. проверка sha256 каждого файла по `manifest.json`;
3. загрузка в базу;
4. запись в `pack_load`.

Повторный запуск с тем же манифестом ничего не делает.

## Что решить на ревью

1. **Имена полей как в v9** — уже так (рекомендация).
2. **Текстовые ID** вместо bigint.
3. **Формат пакета — JSONL.gz,** а не GeoParquet: v9 пишет JSONL, а образу `api` не нужен pyarrow. Быстрый путь `pg_dump` можно добавить позже.
4. **Первая миграция строит схему из моделей** (`create_all`); дальше — обычные ревизии `alembic revision --autogenerate`.
