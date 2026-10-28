# pipeline/

Сборка пакетов городов: build-pack, refresh, coverage.

Владелец: Сева. Изменения в чужой папке — через PR владельца.

- `init.py` — сервис `init`: миграции и загрузка пакетов (`load_packs.py`, потоковый COPY).
  На чистой базе все 8 городов — около 1,5 минуты (замер 27.10 — в README).
- `build_v9_packs.py` — пакеты из расчётов v9: шесть автоматических городов в `packs/`, Москва
  и Сочи — только во внешнюю папку (`--out /tmp/v9`) как вход `build_packs`. Пакет другой сборки
  не перезаписывает.
- Полный каталог Москвы и Сочи: `build_packs.py` (снимок Островка + v9, `--v9-packs`) →
  `calculate_city.py` / `calculate_moscow.py` (георасчёты) → `finalize_packs.py` (фасады, места,
  станции, очередь D33) → `supplement_categories.py` (категории из отдельного снимка) →
  `publish_packs.py` (в `packs/` с резервной копией). Команды — `connectors/README.md`.
- `refresh_places.py` — места и станции готового пакета заново из среза OSM (`connectors.osm_pbf
  --places-only`) без полной пересборки: парки с точками края, подъёмники, граница страны.
  Пакет меняется на месте; отчёт прогона 28.10 — `reports/places_refresh.json`.
- `audit_packs.py`, `audit_ostrovok.py` — офлайн-аудит пакетов и сверка D26 со снимком
  (`make audit`).
- `make_fixtures.py` — заглушки для режима `DATA_MODE=fixtures`.
