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
from sqlalchemy import create_engine

from core.packs import discover
from pipeline.load_packs import load_pack

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("init: DATABASE_URL не задан", file=sys.stderr)
        return 2
    packs_dir = Path(os.environ.get("PACKS_DIR", ROOT / "packs"))

    started = time.monotonic()
    print("init: миграции", flush=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    packs = discover(packs_dir)
    print(f"init: пакетов найдено {len(packs)} в {packs_dir}", flush=True)
    for pack_dir in packs:
        with engine.begin() as conn:
            print("init:", load_pack(conn, pack_dir), flush=True)
    print(f"init: готово за {time.monotonic() - started:.1f} с", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
