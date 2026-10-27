"""Собирает таблицу разметки: случаи Codex + наш ответ + черновик ИИ-проверки.

Заменяет `make_annotations.py`: там был баг — брался первый `fact_id` из списка, поэтому
ноябрьские случаи получали июльское значение. Здесь месяц берётся из идентификатора случая
(`…-11`, `…_7`) и выбирается факт нужного месяца.

Что попадает в таблицу:

- `our_*` — наш ответ из фактов v9;
- `codex_reference`, `reference_url`, `reference_scope` — эталон Codex как есть;
- `ai_*` — черновик проверки из `ai_checks.json`: что нашлось по ссылке и **предложение**
  вердикта. Это не подпись человека;
- `verdict`, `evidence_url`, `evidence_date`, `minutes_spent`, `annotator`, `checked_by`,
  `notes` — пусто, заполняет человек, который открыл доказательство и расписался.
  `checked_by` — кто проверил: человек или, если вердикт сделал ИИ, честно «проверил
  ИИ-ассистент, принято <кто> <дата>» (D33, D41); `minutes_spent` — только засечённые
  человеком минуты.

Запуск:

    python tests/eval/build_annotations.py --out tests/eval/annotations.csv
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "research_notes" / "Расчёт характеристик"
CASES = BASE / "validation" / "cases.csv"
V9_FACTS = BASE / "integrated" / "all8_facts.jsonl.gz"
CHECKS = ROOT / "tests" / "eval" / "ai_checks.json"

HUMAN_COLUMNS = [
    "verdict",
    "evidence_url",
    "evidence_date",
    "minutes_spent",
    "annotator",
    "checked_by",
    "notes",
]
AI_COLUMNS = ["ai_verdict", "ai_check", "ai_evidence_url", "ai_note"]
COLUMNS = [
    "case_id",
    "hotel_id",
    "city_id",
    "hotel_name",
    "feature_id",
    "family",
    "our_status",
    "our_answer",
    "codex_reference",
    "reference_url",
    "reference_scope",
    *AI_COLUMNS,
    *HUMAN_COLUMNS,
]


def fact_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    with gzip.open(V9_FACTS, "rt", encoding="utf-8") as f:
        for line in f:
            fact = json.loads(line)
            index[fact["fact_id"]] = fact
    return index


def case_month(case_id: str) -> int | None:
    """Месяц из идентификатора случая: ENV-C01-10012084-11, STATION_C02_43.5_39.75_7."""
    match = re.search(r"[-_](\d{1,2})$", case_id)
    return int(match.group(1)) if match else None


def pick_fact(facts: dict[str, dict], ids: list[str], month: int | None) -> dict | None:
    candidates = [facts[i] for i in ids if i in facts]
    if month is not None:
        for fact in candidates:
            if (fact.get("params") or {}).get("month") == month:
                return fact
    return candidates[0] if candidates else None


def compact_value(value: object) -> str:
    """Короткая запись значения: для словарей — только смысловые поля."""
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    preferred = (
        "mean",
        "mean_C",
        "count",
        "distance_m",
        "value",
        "level",
        "storey_count",
        "vegetation_percent_land",
    )
    for key in preferred:
        if key in value:
            return f"{key}={value[key]}"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "eval" / "annotations.csv")
    parser.add_argument(
        "--force", action="store_true", help="перезаписать таблицу с уже подписанными вердиктами"
    )
    args = parser.parse_args()

    facts = fact_index()
    checks = json.loads(CHECKS.read_text("utf-8"))["cases"]
    rows = []
    with CASES.open("r", encoding="utf-8", newline="") as f:
        for case in csv.DictReader(f):
            ids = json.loads(case.get("current_prediction_fact_ids") or "[]")
            fact = pick_fact(facts, ids, case_month(case["case_id"]))
            check = checks.get(case["case_id"], {})
            rows.append(
                {
                    "case_id": case["case_id"],
                    "hotel_id": case["hotel_id"],
                    "city_id": case["city_id"],
                    "hotel_name": case["hotel_name"],
                    "feature_id": case["feature_id"],
                    "family": case["family"],
                    "our_status": (fact or {}).get("status", ""),
                    "our_answer": compact_value((fact or {}).get("value")),
                    "codex_reference": case.get("reference_value", ""),
                    "reference_url": case.get("reference_url", ""),
                    "reference_scope": case.get("reference_scope", ""),
                    "ai_verdict": check.get("verdict_proposal", ""),
                    "ai_check": check.get("check", ""),
                    "ai_evidence_url": check.get("evidence_url", ""),
                    "ai_note": check.get("note", ""),
                    **{column: "" for column in HUMAN_COLUMNS},
                }
            )
    if args.out.exists() and not args.force:
        print(f"{args.out} уже есть — вердикты не тронуты (--force, чтобы перезаписать)")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    missing = [r["case_id"] for r in rows if not r["ai_check"]]
    print(f"{args.out}: {len(rows)} случаев; без черновика проверки: {len(missing)}")
    for case_id in missing:
        print("   нет в ai_checks.json:", case_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
