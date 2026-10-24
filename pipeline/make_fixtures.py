"""Заглушки API из настоящих фактов v9 → app/fixtures/. Запуск: `python -m pipeline.make_fixtures`.

Берём отели показа (Сочи и Москва) и все их факты v9, вопросы отельеру с предзаполнением,
покрытие по городам и сравнения с карточкой Островка. Интерфейс сразу видит настоящие данные.
"""

from __future__ import annotations

import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V9 = ROOT / "research_notes" / "Расчёт характеристик"
OUT = ROOT / "app" / "fixtures"

# Отели показа из сценариев (docs/plan.md, раздел 6) + отели Москвы с наибольшим числом расчётов.
DEMO_SOCHI = [
    "13736470",  # Radisson Collection Парадиз
    "10012084",  # Архитектор
    "9986868",  # Cosmos
    "8869683",  # COOCOOROOZA
    "6443737",  # Фрегат
    "11313836",  # «Коралл»
    "6497279",  # Богатырь
    "7631439",  # Аквамарин
    "8740393",  # Аквалоо
    "10575679",  # Бергамо
    "11201577",  # Mantera
]
DEMO_MOSCOW = ["8736052", "6458292"]  # «Прованс», Bolshaya Dorogomilovskaya
MOSCOW_EXTRA = 8
SLIM_KEYS = ("component_diagnostics", "audit_files", "components")  # тяжёлая диагностика v9
CITY_NAMES = {
    "sochi": "Большой Сочи",
    "moscow": "Москва",
    "spb": "Петербург",
    "kazan": "Казань",
    "kaliningrad": "Калининград",
    "minsk": "Минск",
    "istanbul": "Стамбул",
    "dubai": "Дубай",
}
FULL_DEPTH = {"sochi", "moscow"}  # D36


def read_facts() -> list[dict]:
    facts = []
    with gzip.open(V9 / "integrated" / "all8_facts.jsonl.gz", "rt", encoding="utf-8") as f:
        facts.extend(json.loads(line) for line in f if line.strip())
    return facts


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hotels = {
        h["hotel_id"]: h
        for h in map(json.loads, (V9 / "cohort" / "hotels.jsonl").read_text("utf-8").splitlines())
    }
    facts = read_facts()

    good = Counter(
        f["hotel_id"]
        for f in facts
        if f["city_id"] == "moscow" and f["status"] in ("computed", "observed", "estimate")
    )
    moscow = DEMO_MOSCOW + [
        h for h, _ in good.most_common() if h not in DEMO_MOSCOW
    ][:MOSCOW_EXTRA]
    demo = set(DEMO_SOCHI) | set(moscow)

    with gzip.open(OUT / "facts.jsonl.gz", "wt", encoding="utf-8") as out:
        n = 0
        for f in facts:
            if f["hotel_id"] in demo:
                for k in SLIM_KEYS:
                    f.pop(k, None)
                out.write(json.dumps(f, ensure_ascii=False) + "\n")
                n += 1

    keep = ("hotel_id", "city_id", "name", "address", "latitude", "longitude", "hotel_kind", "hotel_url")
    demo_hotels = [{k: hotels[h].get(k) for k in keep} for h in DEMO_SOCHI + moscow if h in hotels]
    (OUT / "hotels.json").write_text(json.dumps(demo_hotels, ensure_ascii=False, indent=1), "utf-8")

    coverage: dict[str, list] = defaultdict(list)
    with (V9 / "integrated" / "all8_coverage.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            coverage[r["city_id"]].append(
                {
                    "feature_id": r["feature_id"],
                    "feature_name": r["feature_name"],
                    "N": int(r["N_hotels"]),
                    "computed": int(r["computed_n"]) + int(r["observed_n"]),
                    "estimate": int(r["estimate_n"]),
                    "unknown": int(r["unknown_n"]),
                    "not_applicable": int(r["not_applicable_n"]),
                }
            )
    cities = [
        {
            "city_id": c,
            "name": CITY_NAMES.get(c, c),
            "depth": "full" if c in FULL_DEPTH else "automatic",
            "hotels_in_sample": rows[0]["N"] if rows else 0,
        }
        for c, rows in coverage.items()
    ]
    (OUT / "cities.json").write_text(json.dumps(cities, ensure_ascii=False, indent=1), "utf-8")
    (OUT / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False), "utf-8")

    questions = defaultdict(list)
    for line in (V9 / "hotelier" / "prefill.jsonl").read_text("utf-8").splitlines():
        q = json.loads(line)
        if q["hotel_id"] in demo:
            questions[q["hotel_id"]].append(q)
    (OUT / "questions.json").write_text(json.dumps(questions, ensure_ascii=False), "utf-8")

    conflicts = []
    with (V9 / "qa" / "writer" / "ota_comparisons_all8.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["hotel_id"] in demo:
                r.pop("snapshot", None)
                conflicts.append(r)
    (OUT / "conflicts.json").write_text(json.dumps(conflicts, ensure_ascii=False, indent=1), "utf-8")

    print(f"fixtures: отелей {len(demo_hotels)}, фактов {n}, сравнений {len(conflicts)}")


if __name__ == "__main__":
    main()
