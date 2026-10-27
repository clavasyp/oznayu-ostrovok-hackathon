"""Прогон калькуляторов среды и климата по городу → JSONL фактов.

Зачем: сборщик пакета города (`pipeline/build_v9_packs.py`) пока берёт только факты v9.
Этот запуск даёт ему свежие факты C01–C03, B02 (и E03, если есть растры) **в том же формате**
`core.facts.Fact`, что и в пакете. В пакет подставлять только посчитанное: факт `unknown` из-за
отсутствия входа не должен заменять значение v9.

Запуск из корня репозитория:

    python -m calculators.env_run --city sochi --out /tmp/sochi_env_facts.jsonl
    python -m calculators.env_run --city moscow --era5-dir "<кеш ERA5>"
    python -m calculators.env_run --city sochi --data-dir "research_notes/Расчёт характеристик"

Где берутся входы:

- отели — когорта v9 (`research_notes/Расчёт характеристик/cohort/hotels.jsonl`) или `--hotels`;
- климат — локальный кеш ответов Open-Meteo `era5_*.json`: `--era5-dir`, иначе первая из папок
  `<data-dir>`, `<data-dir>/era5`, `<data-dir>/environment/raw` (раскладка v9), где он есть;
- море — кеш `marine_*.json` (там же: `<data-dir>`, `<data-dir>/marine/raw`,
  `<data-dir>/environment/raw`) плюс отбор «отель → пляж» от Севы (`--marine-targets`,
  `--marine-mapping`). **Файла отбора Сочи в репозитории нет** (`results/geo` v9 не скопирован):
  без него все факты B02 — `unknown` с причиной `marine_targets_missing`. Это честный
  результат, но не готовый пакет: оценки B02 из пакета такими фактами не заменять.

Сети не касается. Выход детерминированный по составу: те же входы — те же факты (и те же
`fact_id` на любой машине: пути к файлам в ID не входят).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from core.plugins import CityContext, Subject

ROOT = Path(__file__).resolve().parents[1]
V9 = ROOT / "research_notes" / "Расчёт характеристик"
COHORT = V9 / "cohort" / "hotels.jsonl"
ERA5_CACHE = V9 / "environment" / "raw"


def load_subjects(city: str, hotels_path: Path) -> list[Subject]:
    subjects = []
    with hotels_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            hotel = json.loads(line)
            if hotel.get("city_id") != city:
                continue
            subjects.append(
                Subject(
                    hotel_id=hotel["hotel_id"],
                    subject_level="hotel",
                    subject_id=f"hotel:{hotel['hotel_id']}",
                    latitude=hotel.get("latitude"),
                    longitude=hotel.get("longitude"),
                    attrs={"name": hotel.get("name", ""), "subzone": hotel.get("subzone", "")},
                )
            )
    return subjects


def resolve_cache(data_dir: Path, pattern: str, own: str) -> Path | None:
    """Папка с кешем (`era5_*.json`, `marine_*.json`): сама data-dir, подпапка калькулятора
    (`era5`, `marine/raw`) или раскладка v9 `environment/raw`. Не нашли — `None`: тогда
    калькулятор ищет сам и честно отвечает «нет кеша»."""
    for folder in (data_dir, data_dir / own, data_dir / "environment" / "raw"):
        if any(folder.glob(pattern)):
            return folder
    return None


def build_context(args: argparse.Namespace, city: str) -> CityContext:
    params: dict[str, Any] = {}
    era5 = args.era5_dir or resolve_cache(args.data_dir, "era5_*.json", "era5")
    if era5:
        params["era5_dir"] = era5
    marine = resolve_cache(args.data_dir, "marine_*.json", "marine/raw")
    if marine:
        params["marine_cache"] = marine
    if args.marine_targets:
        params["marine_targets"] = args.marine_targets
    if args.marine_mapping:
        params["marine_mapping"] = args.marine_mapping
    if args.worldcover_dir:
        params["worldcover_dir"] = args.worldcover_dir
    return CityContext(
        city_id=city,
        timezone="Europe/Moscow",
        data_dir=args.data_dir,
        params=params,
    )


def calculators(names: list[str]) -> list:
    """Импорт пакета `calculators` регистрирует все калькуляторы в core.plugins.CALCULATORS."""
    import calculators.env_climate  # noqa: F401
    import calculators.env_marine  # noqa: F401
    from core.plugins import CALCULATORS

    available = dict(CALCULATORS)
    if "vegetation" in names:
        missing = [
            module
            for module in ("numpy", "rasterio", "shapely", "pyproj")
            if importlib.util.find_spec(module) is None
        ]
        if missing:
            # E03 уже зарегистрирован импортом пакета: убрать явно, иначе прогон его запустит и
            # вместо пропуска выдаст «неизвестно» по всем отелям.
            print(f"E03 пропущен: нет геобиблиотек ({', '.join(missing)})")
            available.pop("env.vegetation_share", None)
    mapping = {
        "climate": "env.climate_monthly",
        "marine": "env.marine_temperature",
        "vegetation": "env.vegetation_share",
    }
    return [available[mapping[n]] for n in names if mapping[n] in available]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="sochi, moscow, …")
    parser.add_argument("--out", type=Path, required=True, help="куда писать JSONL фактов")
    parser.add_argument("--hotels", type=Path, default=COHORT, help="JSONL отелей когорты")
    parser.add_argument("--data-dir", type=Path, default=ERA5_CACHE, help="кеш ERA5 и моря")
    parser.add_argument("--era5-dir", type=Path, default=None, help="кеш ERA5 отдельно")
    parser.add_argument("--marine-targets", type=Path, default=None, help="отбор отель → пляж")
    parser.add_argument("--marine-mapping", type=Path, default=None, help="карта пляж → ячейка")
    parser.add_argument("--worldcover-dir", type=Path, default=None, help="тайлы WorldCover")
    parser.add_argument(
        "--only",
        nargs="+",
        default=["climate", "marine"],
        choices=["climate", "marine", "vegetation"],
        help="какие семейства считать",
    )
    args = parser.parse_args()

    subjects = load_subjects(args.city, args.hotels)
    if not subjects:
        print(f"нет отелей города {args.city} в {args.hotels}")
        return 1
    ctx = build_context(args, args.city)
    for key in ("era5_dir", "marine_cache", "marine_targets", "marine_mapping"):
        print(f"{key}: {ctx.params.get(key) or '— (калькулятор ищет сам)'}")

    facts: list = []
    for calculator in calculators(args.only):
        produced = calculator.run(subjects, ctx)
        print(f"{calculator.id}: {len(produced)} фактов")
        facts.extend(produced)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for fact in facts:
            f.write(
                json.dumps(fact.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            )

    by_feature: dict[str, Counter] = defaultdict(Counter)
    for fact in facts:
        by_feature[fact.feature_id][str(fact.status)] += 1
    print(f"\n{args.city}: отелей {len(subjects)}, фактов {len(facts)} → {args.out}")
    for feature, counts in sorted(by_feature.items()):
        print(f"  {feature}: {dict(counts)}")
    unknown = Counter(f.unknown_reason for f in facts if f.unknown_reason)
    if unknown:
        print("  причины «неизвестно»:")
        for reason, count in unknown.most_common():
            print(f"    {count:4}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
