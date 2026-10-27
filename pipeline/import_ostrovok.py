"""Пилот «снимок → каталог/факты → пакет → отдельная тестовая PostGIS».

python -m pipeline.import_ostrovok --snapshot /data/snapshot_2026-10-24 \
    --out /tmp/ostrovok-pilot --limit-per-city 5

Существующие packs/ не изменяются. Географический отбор и объединение с v9 — этап 3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from connectors.ostrovok_site import VERSION, Catalog, CityId, read_snapshot
from core.packs import Manifest, PackFile, PackSource, discover, sha256_file, verify
from pipeline.build_v9_packs import CITY_NAMES, ROOT, write_jsonl_gz


def build_pilot(
    snapshot: Path,
    out: Path,
    *,
    limit_per_city: int = 5,
    cities: tuple[CityId, ...] = ("moscow", "sochi"),
) -> dict:
    """Только новый пустой выход; пилот не может заменить штатные пакеты или снимок."""
    snapshot, out = snapshot.resolve(), out.resolve()
    packs = (ROOT / "packs").resolve()
    for protected in (packs, snapshot):
        if out.is_relative_to(protected) or protected.is_relative_to(out):
            raise ValueError("Выход пилота пересекается со штатными пакетами или снимком")
    if out.is_relative_to(ROOT.resolve()):
        raise ValueError("Выход пилота должен находиться вне репозитория")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Выход пилота должен быть новым или пустым каталогом")
    if limit_per_city < 1 or not cities or len(set(cities)) != len(cities):
        raise ValueError("Нужны уникальные города и положительный лимит")
    catalogs: dict[str, Catalog] = {}
    seen_hotels = set()
    for city in cities:
        catalog = read_snapshot(snapshot, city, limit_per_city)
        if not catalog.hotels:
            raise ValueError(f"{city}: нет пригодных страниц; пакет не создан")
        ids = {h.id for h in catalog.hotels}
        if ids & seen_hotels:
            raise ValueError(
                "Один masterId встретился в разных рынках; требуется географический отбор"
            )
        seen_hotels.update(ids)
        catalogs[city] = catalog
    report = {
        "algorithm_version": VERSION,
        "pilot": True,
        "geography_verified": False,
        "limit_per_city": limit_per_city,
        "cities": {},
    }
    for city, catalog in catalogs.items():
        parts = {
            "hotels": sorted([h.model_dump() for h in catalog.hotels], key=lambda r: r["id"]),
            "room_types": sorted(
                [r.model_dump() for r in catalog.room_types], key=lambda r: r["id"]
            ),
            "facts": sorted(
                [f.model_dump(mode="json", exclude_none=True) for f in catalog.facts],
                key=lambda r: r["fact_id"],
            ),
        }
        identity = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
        pack_version = f"ostrovok-pilot-{hashlib.sha256(identity).hexdigest()[:12]}"
        data = out / city / "data"
        data.mkdir(parents=True)
        files = []
        for kind, rows in parts.items():
            path = data / f"{kind}.jsonl.gz"
            write_jsonl_gz(path, rows)
            files.append(
                PackFile(
                    path=f"data/{path.name}",
                    kind=kind,
                    rows=len(rows),
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
        built_at = max(datetime.fromisoformat(h.source["fetched_at"]) for h in catalog.hotels)
        manifest = Manifest(
            city_id=city,
            city_name=CITY_NAMES[city],
            timezone="Europe/Moscow",
            depth="full",
            pack_version=pack_version,
            built_at=built_at,
            code_version=VERSION,
            catalog_version=snapshot.name,
            sources=[
                PackSource(
                    id="ostrovok_site",
                    provider="ostrovok.ru",
                    release=snapshot.name,
                    sha256=sha256_file(snapshot / "manifests/pages.jsonl"),
                    fetched_at=built_at,
                )
            ],
            files=files,
            notes=(
                "Пилот, не полный каталог. Город — метка рынка из журнала; границы и "
                "подзоны ещё не проверены. Частичные заявления категории не являются "
                "гарантией всех вариантов. Не заменять этим пакетом рабочий город."
            ),
        )
        (out / city / "manifest.json").write_text(
            manifest.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8"
        )
        verify(out / city)
        report["cities"][city] = {
            "hotels": len(catalog.hotels),
            "room_types": len(catalog.room_types),
            "facts": len(catalog.facts),
            "excluded": catalog.excluded,
            "pack_version": pack_version,
        }
    (out / "import_report.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return report


def load_test_database(out: Path, database_url: str) -> list[str]:
    """Штатный загрузчик, только в БД *_test с уже применёнными миграциями."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from pipeline.load_packs import load_pack

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
        raise ValueError("Пилот разрешено загружать только в отдельную PostgreSQL БД *_test")
    packs = discover(out)
    if not packs:
        raise ValueError("В выходном каталоге нет пакетов")
    # Сначала проверка всех файлов, затем одна транзакция на оба города.
    for pack in packs:
        manifest = verify(pack)
        if not manifest.pack_version.startswith("ostrovok-pilot-"):
            raise ValueError("Ожидался пакет пилота Островка")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            return [load_pack(conn, pack) for pack in packs]
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit-per-city", type=int, default=5)
    parser.add_argument("--city", choices=("moscow", "sochi"), action="append")
    parser.add_argument(
        "--load-test-db",
        action="store_true",
        help="загрузить в TEST_DATABASE_URL; только отдельная БД *_test",
    )
    args = parser.parse_args()
    try:
        database_url = os.environ.get("TEST_DATABASE_URL")
        if args.load_test_db and not database_url:
            raise ValueError("Для --load-test-db требуется TEST_DATABASE_URL")
        report = build_pilot(
            args.snapshot,
            args.out,
            limit_per_city=args.limit_per_city,
            cities=tuple(args.city) if args.city else ("moscow", "sochi"),
        )
        if args.load_test_db:
            report["database_load"] = load_test_database(args.out, database_url)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Импорт Островка: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
