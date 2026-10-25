"""Точность разбора запроса на эталонах (`tests/eval/parse_reference.jsonl`).

Эталон: у каждого запроса — ожидаемые обязательные и желательные условия; элемент — список
допустимых ID характеристик, `ooc` — допустима часть «вне каталога» (D34).

Совпадение: условие с ID из списка или часть вне каталога с этим ID среди ближайших. Условия из
одного фрагмента запроса считаются одним (у «тихий» — N02 и N04). Главная метрика —
**потерянное обязательное**: ожидалось в hard, а в разборе его нет нигде.

Запуск: `python -m agents.eval.parse_eval --mode rules` (без сети) или `--mode llm` (с ключом,
стоит денег). Отчёт — `tests/eval/reports/parse_<mode>.{json,md}`.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from agents.query_ast import QueryAST
from agents.search import parse_query

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "tests" / "eval" / "parse_reference.jsonl"
REPORTS = ROOT / "tests" / "eval" / "reports"
TODAY = date(2026, 10, 25)  # фиксировано: даты без года считаются от этого дня


@dataclass
class Item:
    span: tuple[int, int] | None
    features: set[str]  # ID условия или ближайшие у части вне каталога (+ "ooc")


def produced(ast: QueryAST) -> tuple[list[Item], list[Item]]:
    """Обязательные и желательные элементы разбора, сгруппированные по фрагменту запроса."""
    required_ooc_atoms = {
        o.atom_id for o in ast.out_of_catalog if o.requiredness == "required" and o.atom_id
    }
    hard: dict[Any, Item] = {}
    soft: dict[Any, Item] = {}

    def add(bucket: dict[Any, Item], span, features: set[str]) -> None:
        key = (span.start, span.end) if span else id(features)
        item = bucket.setdefault(key, Item(key if span else None, set()))
        item.features |= features

    for c in ast.hard:
        add(hard, c.span, {c.feature_id})
    for c in ast.soft:
        if c.atom_id not in required_ooc_atoms:  # частичное обязательное уже учтено как ooc
            add(soft, c.span, {c.feature_id})
    for o in ast.out_of_catalog:
        bucket = hard if o.requiredness == "required" else soft
        add(bucket, o.span, {"ooc", *o.nearest_contracts})
    return list(hard.values()), list(soft.values())


def match(expected: list[list[str]], items: list[Item]) -> tuple[int, list[list[str]], list[Item]]:
    """Жадное сопоставление один к одному: (совпало, не найдено, лишнее)."""
    free = list(items)
    missed = []
    hits = 0
    for e in expected:
        found = next((it for it in free if it.features & set(e)), None)
        if found is None:
            missed.append(e)
        else:
            hits += 1
            free.remove(found)
    return hits, missed, free


def evaluate_case(case: dict[str, Any], mode: str) -> dict[str, Any]:
    started = time.monotonic()
    outcome = parse_query(case["text"], case.get("city_id"), today=TODAY, mode=mode)
    elapsed = round((time.monotonic() - started) * 1000)
    ast = outcome.ast
    hard_items, soft_items = produced(ast)
    extra_ok = set(case.get("hard_extra_ok", []))
    h_tp, h_missed, h_extra = match(case["hard"], hard_items)
    h_extra = [it for it in h_extra if not it.features & extra_ok]
    s_tp, s_missed, s_extra = match(case["soft"], soft_items)
    # Обязательное не найдено среди обязательных: понижено до желательного или потеряно.
    _, lost, _ = match(h_missed, soft_items)
    downgraded = len(h_missed) - len(lost)
    return {
        "id": case["id"],
        "parsed_by": ast.parsed_by,
        "notes": outcome.notes,
        "problems": outcome.problems,
        "ms": elapsed,
        "usage": outcome.llm.usage if outcome.llm else {},
        "hard": {"tp": h_tp, "fn": len(h_missed), "fp": len(h_extra), "missed": h_missed},
        "soft": {"tp": s_tp, "fn": len(s_missed), "fp": len(s_extra), "missed": s_missed},
        "extra_hard": [sorted(it.features) for it in h_extra],
        "extra_soft": [sorted(it.features) for it in s_extra],
        "lost_required": lost,
        "downgraded_required": downgraded,
        "unparsed": [s.text for s in ast.unparsed_spans],
        "exact_hard": not h_missed and not h_extra,
    }


def _prf(tp: int, fn: int, fp: int) -> dict[str, float | None]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {"precision": _r(p), "recall": _r(r)}


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def summarize(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    tot = {k: {m: sum(r[k][m] for r in rows) for m in ("tp", "fn", "fp")} for k in ("hard", "soft")}
    tokens = {
        k: sum(r["usage"].get(k, 0) for r in rows)
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")
    }
    return {
        "mode": mode,
        "cases": len(rows),
        "parsed_by": {p: sum(r["parsed_by"] == p for r in rows) for p in ("rules", "llm")},
        "hard": tot["hard"] | _prf(**tot["hard"]),
        "soft": tot["soft"] | _prf(**tot["soft"]),
        "exact_hard_cases": sum(r["exact_hard"] for r in rows),
        "lost_required": sum(len(r["lost_required"]) for r in rows),
        "downgraded_required": sum(r["downgraded_required"] for r in rows),
        "cases_with_unparsed": sum(bool(r["unparsed"]) for r in rows),
        "cases_with_problems": sum(bool(r["problems"]) for r in rows),
        "median_ms": sorted(r["ms"] for r in rows)[len(rows) // 2] if rows else None,
        "tokens": tokens,
    }


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    s = summary
    lines = [
        f"# Разбор запроса: режим `{s['mode']}`, {s['cases']} эталонов",
        "",
        "Эталоны — `tests/eval/parse_reference.jsonl`: 30 из отчёта v1 (блок 4) и сценарии показа.",
        "",
        *(
            [
                "⚠ Правила писались по этим же запросам: цифры режима `rules` — защита от "
                "регрессий, а не оценка качества. Оценка — режим `llm` и независимый набор v11.",
                "",
            ]
            if s["mode"] == "rules"
            else []
        ),
        "| Метрика | Значение |",
        "|---|---|",
        f"| Обязательные: точность / полнота | {s['hard']['precision']} / {s['hard']['recall']} |",
        f"| Желательные: точность / полнота | {s['soft']['precision']} / {s['soft']['recall']} |",
        f"| Запросов с точным набором обязательных | {s['exact_hard_cases']} из {s['cases']} |",
        f"| **Потеряно обязательных** | **{s['lost_required']}** |",
        f"| Обязательных понижено до желательных | {s['downgraded_required']} |",
        f"| Запросов с неразобранным остатком | {s['cases_with_unparsed']} |",
        f"| Разобрано правилами / моделью | {s['parsed_by']['rules']} / {s['parsed_by']['llm']} |",
        f"| Медиана времени, мс | {s['median_ms']} |",
        f"| Токены (вход / выход / из кэша) | {s['tokens']['input_tokens']} / "
        f"{s['tokens']['output_tokens']} / {s['tokens']['cache_read_input_tokens']} |",
        "",
        "## По запросам",
        "",
        "| ID | Кто | Обяз. ✓/✗/лишн. | Желат. ✓/✗/лишн. | Потеряно | Не разобрано |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        h, so = r["hard"], r["soft"]
        lines.append(
            f"| {r['id']} | {r['parsed_by']} | {h['tp']}/{h['fn']}/{h['fp']} | "
            f"{so['tp']}/{so['fn']}/{so['fp']} | {r['lost_required'] or ''} | "
            f"{'; '.join(r['unparsed'])} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["rules", "llm", "auto"], default="rules")
    ap.add_argument("--ids", nargs="*", help="только эти эталоны")
    ap.add_argument("--out", type=Path, default=REPORTS)
    args = ap.parse_args(argv)
    cases = [json.loads(line) for line in REFERENCE.read_text("utf-8").splitlines() if line]
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids]
    rows = [evaluate_case(c, args.mode) for c in cases]
    summary = summarize(rows, args.mode)
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary, "cases": rows}
    (args.out / f"parse_{args.mode}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8"
    )
    (args.out / f"parse_{args.mode}.md").write_text(markdown(summary, rows), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return report


if __name__ == "__main__":
    main()
