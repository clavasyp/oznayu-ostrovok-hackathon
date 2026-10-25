# pipeline/

Сборка пакетов городов: build-pack, refresh, coverage.

Владелец: Сева. Изменения в чужой папке — через PR владельца.

- `init.py` — сервис `init`: миграции и загрузка пакетов (`load_packs.py`).
- `build_v9_packs.py` — пакеты 8 городов из расчётов v9.
- `make_fixtures.py` — заглушки для режима `DATA_MODE=fixtures`.
