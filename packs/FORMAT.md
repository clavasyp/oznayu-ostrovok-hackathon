# Формат пакета города (версия 1)

Пакет — готовые результаты по городу. Собирается заранее, вне запуска жюри; при старте сервис `init` проверяет хеши и грузит пакет в базу (D6, core.md §9). Код — `core/packs.py`, загрузка — `pipeline/load_packs.py`.

```
packs/<city_id>/
  manifest.json
  data/
    hotels.jsonl.gz             отели: id (masterId), city_id, name, address, kind, stars, subzone, latitude, longitude, source
    room_types.jsonl.gz         категории: id = "<hotel_id>:<rgHash>", hotel_id, rg_hash, name, amenities, source
    placement_sets.jsonl.gz     размещения категорий (D17): room_type_id, completeness, guarantee, status, source
    placements.jsonl.gz         размещение: id, room_type_id, building_id, floor, floor_label
    placement_windows.jsonl.gz  окна размещения: placement_id, facade_id, azimuth_deg
    buildings.jsonl.gz          корпуса: id ("way/…"), city_id, geometry (GeoJSON), tags
    hotel_buildings.jsonl.gz    связи: hotel_id, building_id, method (auto | curated), evidence
    facts.jsonl.gz              факты — ровно в формате core/facts.py (как факты v9)
    questions.jsonl.gz          вопросы отельеру с предзаполнением: id, hotel_id, feature_id, text, prefill, fields, state
    coverage.jsonl.gz           покрытие по характеристикам: feature_id, n_hotels, computed, estimate, unknown, not_applicable
    ota_comparisons.jsonl.gz    сверка с карточкой Островка: hotel_id, feature_id, ota_value, classification, source
```

**Правила:**
- Строка JSONL = одна запись, UTF-8, gzip.
- Факт проходит валидацию `core.facts.Fact`: у `unknown` есть `unknown_reason`, у статуса со значением есть `value`.
- **Окно** — субъект фактов вида, солнца и шума у окна: `subject_level = facade_physical_floor`, `subject_id = "<building_id>:<facade_id>:floor<N>"`, как в расчётах v9 (`facades.py`). Размещение категории ссылается на те же здание, фасад и этаж — так движок поиска находит факты окна (D17).
- **Категория** — субъект фактов уровня номера (кровати, балкон, Wi-Fi): `subject_level = room_type`, `subject_id = "<hotel_id>:<rgHash>"`.
- Новая версия пакета заменяет данные города целиком; факты, созданные в работе, остаются (`pipeline/load_packs.py`).
- В пакет идут только результаты и минимальная геометрия для карты. Сырые PBF, растры, графы и кеши API — нет.
- Источники с запретом распространения в пакет не попадают (`commercial_ok` в `manifest.json`).

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
