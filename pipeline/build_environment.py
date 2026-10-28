"""Слой «среда» на все отели пакета: климат C01–C03 (ERA5 через Open-Meteo) и доля зелени E03
(ESA WorldCover 2021) — те же калькуляторы, что для когорты v9 (`calculators.env_*`).

python -m pipeline.build_environment --city sochi --era5 "<кеш v9>" <докачанный кеш> \
    --worldcover-dir "<тайлы WorldCover>"

Отели, у которых характеристика уже посчитана в пакете (когорта v9), пропускаются: те же входы
дали бы те же `fact_id`. Пишет `packs/<город>/data/environment.jsonl.gz` и добавляет его в манифест
(`add_layer`); сборка каталога слой сохраняет (`keep_layers`).
"""

from __future__ import annotations

import argparse
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from calculators.env_run import calculators
from core.packs import PackSource, add_layer, iter_jsonl, read_manifest
from core.plugins import CityContext, Subject
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/environment.jsonl.gz"
FAMILIES = {"climate": ("C01", "C02", "C03"), "vegetation": ("E03",)}
SOURCES = [
    PackSource(
        id="environment:open-meteo-era5",
        provider="Open-Meteo Historical Weather API (ERA5)",
        url="https://open-meteo.com/en/docs/historical-weather-api",
        license="CC BY 4.0; бесплатный API — только разовая сборка на хакатоне (D22)",
        commercial_ok=False,
    ),
    PackSource(
        id="environment:esa-worldcover-2021",
        provider="ESA WorldCover",
        url="https://esa-worldcover.org/",
        release="v200 2021",
        license="CC BY 4.0",
        commercial_ok=True,
    ),
]


# Что у файлов кеша разное, кроме адреса запроса (он восстанавливается по `*.meta.json` файла).
PER_FILE = ("snapshot_file", "dataset_sha256", "start_date", "json_pointer")


def compact_sources(row: dict) -> dict:
    """Климат: 30 файлов кеша (по году) — один источник со списками файлов и их sha256.

    Без этого паспорт факта — 30 почти одинаковых записей по ~1 КБ, слой на все отели весит
    сотни мегабайт. Общие поля остаются как есть; по спискам каждый файл находится в кеше."""
    sources = row.get("sources") or []
    if len(sources) <= 1:
        return row
    first = sources[0]
    common = {k: v for k, v in first.items() if all(s.get(k) == v for s in sources)}
    merged = common | {f"{k}_list": [s.get(k) for s in sources] for k in PER_FILE}
    fetched = sorted(s["fetched_at"] for s in sources if s.get("fetched_at"))
    merged |= {"fetched_at": fetched[-1] if fetched else None, "merged_sources": len(sources)}
    return row | {"sources": [merged]}


def merged_cache(dirs: list[Path], into: Path) -> Path:
    """Калькулятор климата читает одну папку: собираем ссылки на файлы всех кешей."""
    for d in dirs:
        for path in d.glob("era5_*.json"):
            link = into / path.name
            if not link.exists():
                link.symlink_to(path.resolve())
    return into


def pack_rows(pack: Path, kind: str) -> Iterator[dict]:
    """Строки всех файлов вида из манифеста, кроме этого слоя (его пересобираем)."""
    for f in read_manifest(pack).files:
        if f.kind == kind and f.path != LAYER:
            yield from iter_jsonl(pack / f.path)


def build(
    city: str, era5: list[Path], worldcover: Path, packs: Path = PACKS, points: bool = False
) -> list[dict]:
    pack = packs / city
    done: dict[str, set[str]] = {}  # характеристика → отели, у которых она уже есть
    existing = set()
    for fact in pack_rows(pack, "facts"):
        existing.add(fact["fact_id"])
        if fact["status"] != "unknown" and any(fact["feature_id"] in f for f in FAMILIES.values()):
            done.setdefault(fact["feature_id"], set()).add(fact["hotel_id"])
    hotels = list(pack_rows(pack, "hotels"))
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = CityContext(
            city_id=city,
            timezone="Europe/Moscow",
            data_dir=Path(tmp),
            params={
                "era5_dir": merged_cache(era5, Path(tmp)),
                "worldcover_dir": worldcover,
                "climate_points": points,
            },  # fmt: skip
        )
        for family, features in FAMILIES.items():
            skip = set.intersection(*(done.get(f, set()) for f in features))
            subjects = [
                Subject(
                    hotel_id=h["id"],
                    subject_level="hotel",
                    subject_id=f"hotel:{h['id']}",
                    latitude=h["latitude"],
                    longitude=h["longitude"],
                    attrs={"name": h.get("name", ""), "subzone": h.get("subzone") or ""},
                )
                for h in hotels
                if h["id"] not in skip
            ]
            for calculator in calculators([family]):
                rows += [compact_sources(fact_row(f)) for f in calculator.run(subjects, ctx)]
    clash = [r["fact_id"] for r in rows if r["fact_id"] in existing]
    if clash:
        raise SystemExit(f"{city}: {len(clash)} фактов уже есть в пакете: {clash[:3]}")
    rows.sort(key=lambda r: (r["hotel_id"], r["feature_id"], str(r["params"]), r["fact_id"]))
    (pack / LAYER).parent.mkdir(exist_ok=True)
    write_jsonl_gz(pack / LAYER, rows)
    add_layer(pack, LAYER, "facts", len(rows), SOURCES)
    stats = Counter((r["feature_id"], r["status"], r["unknown_reason"]) for r in rows)
    print(f"{city}: {len(rows)} фактов, {(pack / LAYER).stat().st_size / 1e6:.1f} МБ")
    for key, n in sorted(stats.items(), key=str):
        print(f"  {key}: {n}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", choices=("moscow", "sochi"), nargs="+", default=["moscow", "sochi"])
    ap.add_argument("--era5", type=Path, nargs="+", required=True, help="папки кеша ERA5")
    ap.add_argument("--worldcover-dir", type=Path, required=True, help="тайлы WorldCover 3°×3°")
    ap.add_argument("--points", action="store_true", help="климат по точкам у отелей "
                    "(кеш `fetch_era5 --points`)")  # fmt: skip
    args = ap.parse_args()
    for city in args.city:
        build(city, args.era5, args.worldcover_dir, points=args.points)


if __name__ == "__main__":
    main()
