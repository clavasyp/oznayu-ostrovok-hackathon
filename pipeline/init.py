"""Команда сервиса `init`: миграции + загрузка пакетов городов. Запуск: `python -m pipeline.init`.

Работает без сети: только проверка хешей и загрузка готовых результатов (правило «≤ 10 минут»).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from core.packs import discover
from pipeline.load_packs import load_pack

ROOT = Path(__file__).resolve().parents[1]


def wait_for_db(engine: Engine, timeout_s: float = 120) -> None:
    """База могла ещё не принять соединения (первый запуск, медленный диск, эмуляция amd64)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with engine.connect() as conn:
                conn.execute(text("select 1"))
            return
        except OperationalError as exc:
            if time.monotonic() > deadline:
                raise
            print(f"init: жду базу ({type(exc.orig).__name__})", flush=True)
            time.sleep(2)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("init: DATABASE_URL не задан", file=sys.stderr)
        return 2
    packs_dir = Path(os.environ.get("PACKS_DIR", ROOT / "packs"))

    started = time.monotonic()
    engine = create_engine(url)
    wait_for_db(engine)
    print("init: миграции", flush=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    packs = discover(packs_dir)
    print(f"init: пакетов найдено {len(packs)} в {packs_dir}", flush=True)
    if not packs:
        print("init: пакетов пока нет: fixtures — заглушки v9, db — ждёт пакеты", flush=True)
    for pack_dir in packs:
        city_started = time.monotonic()
        with engine.begin() as conn:
            # Прогресс по видам файлов: на Москве init иначе молчит минутами.
            result = load_pack(
                conn, pack_dir, progress=lambda line: print("init:", line, flush=True)
            )
        print(f"init: {result} — {time.monotonic() - city_started:.1f} с", flush=True)
    print(f"init: готово за {time.monotonic() - started:.1f} с", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
