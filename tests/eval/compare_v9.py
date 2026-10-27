"""Сверка калькуляторов Леры с расчётами v9 на общих отелях.

Запуск (из корня репозитория):

    python tests/eval/compare_v9.py --cities sochi moscow --features C01 C02 C03

Что делает: считает факты текущими калькуляторами по когорте v9
(`research_notes/Расчёт характеристик/cohort/hotels.jsonl`) и сравнивает значения с фактами
v9 (`research_notes/Расчёт характеристик/integrated/all8_facts.jsonl.gz`) по ключу
(отель, характеристика, месяц). Печатает таблицу расхождений и код возврата 1, если
расхождения есть.

Это не метрика качества: v9 — не независимый эталон (см. `docs/research/review_gpt_v9_sochi.md`).
Это проверка переноса: «тот же вход — то же число».
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from calculators.env_climate import ClimateMonthly  # noqa: E402
from core.plugins import CityContext, Subject  # noqa: E402

BASE = ROOT / "research_notes" / "Расчёт характеристик"
COHORT = BASE / "cohort" / "hotels.jsonl"
V9_FACTS = BASE / "integrated" / "all8_facts.jsonl.gz"
ERA5 = BASE / "environment" / "raw"

# Поля значения, которые обязаны совпасть (числа — с допуском, см. TOLERANCE).
SCALARS = ("mean", "stddev_across_years", "minimum_annual_value", "maximum_annual_value")
COUNTS = ("n_years", "valid_days")
TOLERANCE = 1e-9


def cohort_subjects(cities: set[str]) -> list[Subject]:
    subjects = []
    for line in COHORT.open("r", encoding="utf-8"):
        h = json.loads(line)
        if h["city_id"] not in cities:
            continue
        subjects.append(
            Subject(
                hotel_id=h["hotel_id"],
                subject_level="hotel",
                subject_id=f"hotel:{h['hotel_id']}",
                latitude=h["latitude"],
                longitude=h["longitude"],
                attrs={"city_id": h["city_id"]},
            )
        )
    return subjects


def v9_values(features: set[str], cities: set[str]) -> dict[tuple, dict]:
    values: dict[tuple, dict] = {}
    with gzip.open(V9_FACTS, "rt", encoding="utf-8") as f:
        for line in f:
            fact = json.loads(line)
            if fact["feature_id"] not in features or fact["city_id"] not in cities:
                continue
            month = fact.get("params", {}).get("month")
            values[(fact["hotel_id"], fact["feature_id"], month)] = fact
    return values


def run(cities: list[str], features: list[str]) -> int:
    calculator = ClimateMonthly()
    ours: dict[tuple, dict] = {}
    for city in cities:
        subjects = cohort_subjects({city})
        ctx = CityContext(
            city_id=city, timezone="Europe/Moscow", data_dir=ERA5, params={"era5_dir": ERA5}
        )
        for fact in calculator.run(subjects, ctx):
            if fact.feature_id not in features:
                continue
            ours[(fact.hotel_id, fact.feature_id, fact.params.get("month"))] = fact
    v9 = v9_values(set(features), set(cities))

    common = sorted(set(ours) & set(v9))
    only_ours = sorted(set(ours) - set(v9))
    only_v9 = sorted(set(v9) - set(ours))
    mismatches: list[tuple] = []
    status_diff: list[tuple] = []
    max_diff = defaultdict(float)
    for key in common:
        a, b = ours[key], v9[key]
        if str(a.status) != b["status"]:
            status_diff.append((key, str(a.status), b["status"]))
        av, bv = a.value or {}, b.get("value") or {}
        for field in SCALARS:
            if field not in av and field not in bv:
                continue
            diff = abs(float(av.get(field, 0)) - float(bv.get(field, 0)))
            max_diff[field] = max(max_diff[field], diff)
            if diff > TOLERANCE:
                mismatches.append((key, field, av.get(field), bv.get(field)))
        for field in COUNTS:
            if av.get(field) != bv.get(field):
                mismatches.append((key, field, av.get(field), bv.get(field)))
        if av.get("annual_values") != bv.get("annual_values"):
            mismatches.append((key, "annual_values", "…", "…"))

    print(f"общих фактов: {len(common)}")
    print(f"только у нас: {len(only_ours)}, только в v9: {len(only_v9)}")
    print("макс. |разница| по полям:", {k: round(v, 12) for k, v in sorted(max_diff.items())})
    print(f"расхождений значений: {len(mismatches)}")
    for row in mismatches[:10]:
        print("  ", row)
    print(f"расхождений статуса: {len(status_diff)}")
    for row in status_diff[:10]:
        print("  ", row)
    for key in only_v9[:5]:
        print("   нет у нас:", key)
    for key in only_ours[:5]:
        print("   нет в v9:", key)
    ok = not mismatches and not status_diff and not only_v9 and not only_ours
    print("ИТОГ:", "совпадает с v9" if ok else "есть расхождения")
    return 0 if ok else 1


def run_marine(cities: list[str]) -> int:
    """Сверка численного ядра B02 с v9 по тем же ячейкам модели.

    Отбор «отель → пляж» в v9 делала геометрия Севы; здесь он берётся из фактов v9, чтобы
    проверить именно расчёт (2023–2025, нативные 6-часовые шаги). Когда Сева отдаст свой
    отбор, поменяется только вход, а не арифметика.
    """
    from calculators.env_marine import monthly_mean

    v9 = v9_values({"B02"}, set(cities))
    compared = 0
    mismatches: list[tuple] = []
    not_compared: list[tuple] = []
    for key, fact in sorted(v9.items()):
        params = fact.get("params") or {}
        if fact["status"] != "estimate":
            not_compared.append((key, fact["status"]))
            continue
        cell = (float(params["api_reported_latitude"]), float(params["api_reported_longitude"]))
        value, _sources, error = monthly_mean(ERA5, fact["city_id"], cell, params["month"])
        if value is None:
            mismatches.append((key, "unknown", error))
            continue
        compared += 1
        for field in ("mean_C", "stddev_across_years_C"):
            if abs(value[field] - fact["value"][field]) > TOLERANCE:
                mismatches.append((key, field, value[field], fact["value"][field]))
        if value["annual_monthly_means_C"] != fact["value"]["annual_monthly_means_C"]:
            mismatches.append((key, "annual_monthly_means_C", value, fact["value"]))
        if value["native_6hour_completeness"] != fact["value"]["native_6hour_completeness"]:
            mismatches.append((key, "native_6hour_completeness", value, fact["value"]))
    print(f"сверено фактов B02: {compared}, не сверялось: {len(not_compared)}")
    print(f"расхождений: {len(mismatches)}")
    for row in mismatches[:10]:
        print("  ", row)
    return 0 if not mismatches else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", nargs="+", default=["sochi", "moscow"])
    parser.add_argument("--features", nargs="+", default=["C01", "C02", "C03"])
    args = parser.parse_args()
    status = 0
    climate = [f for f in args.features if f in {"C01", "C02", "C03"}]
    if climate:
        status |= run(args.cities, climate)
    if "B02" in args.features:
        status |= run_marine(args.cities)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
