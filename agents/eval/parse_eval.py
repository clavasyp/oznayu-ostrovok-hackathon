"""Точность разбора запроса на эталонах (`tests/eval/parse_reference.jsonl`).

Эталон: у каждого запроса — ожидаемые обязательные и желательные условия; элемент — список
допустимых ID характеристик, `ooc` — допустима часть «вне каталога» (D34). ID с направлением —
`"N04 lte 2"` (ID, оператор, значение) или `"N04 gte"` (только направление: порог политики):
так сверяется и сторона условия, а не только код — «мало баров» не засчитается за «много».

Совпадение: условие с ID из списка или часть вне каталога с этим ID среди ближайших. Условия из
одного фрагмента запроса считаются одним (у «тихий» — N02 и N04). Главная метрика —
**потерянное обязательное**: ожидалось в hard, а в разборе его нет нигде.

Запуск: `python -m agents.eval.parse_eval --mode rules` (без сети) или `--mode llm` (с ключом,
стоит денег). Отчёт — `tests/eval/reports/parse_<mode>.{json,md}`. С `--csv FILE` — ещё строка
на каждое условие (`query_id, atom_id, expected, got, requiredness`) для общей метрики
`tests/eval/metrics.py --parse` (Лера): совпало — `got` = `expected`, не нашлось — `got` пуст,
лишнее — пуст `expected`. Доля совпавших строк — не точность и не полнота (в строках и пропуски,
и лишнее): точность и полнота — в отчёте `parse_<mode>.md`.

Режим `rules` детерминирован: `tests/eval/parse_atoms_rules.csv` и `reports/parse_rules.*`
сверяет тест — после правки правил или эталонов их перевыгружают:
`python -m agents.eval.parse_eval --mode rules --csv tests/eval/parse_atoms_rules.csv`.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from agents.llm import LLMSettings
from agents.query_ast import QueryAST
from agents.search import parse_query
from agents.search.compact import is_leftover

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "tests" / "eval" / "parse_reference.jsonl"
REPORTS = ROOT / "tests" / "eval" / "reports"
TODAY = date(2026, 10, 25)  # фиксировано: даты без года считаются от этого дня


@dataclass
class Item:
    span: tuple[int, int] | None
    features: set[str]  # ID условия или ближайшие у части вне каталога (+ "ooc")
    leftover: bool = False  # непонятый кусок рядом с «обязательно» (не атом разбора)
    atoms: list[str] = field(default_factory=list)  # atom_id разбора в этом фрагменте
    text: str = ""
    conds: set[tuple[str, str, Any]] = field(default_factory=set)  # (ID, оператор, значение)


_DIRECTION = {"lte": "le", "lt": "le", "gte": "ge", "gt": "ge"}


def _cond(op: str, value: Any) -> tuple[str, Any]:
    """Условие для сверки с эталоном: число — числом, у целых «< 2» — то же, что «≤ 1»;
    множество — отсортированным кортежем, «true» / «false» — да / нет."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return op, low == "true"
        if op == "in":
            return op, tuple(sorted(v.strip() for v in value.split(",") if v.strip()))
        try:
            value = float(low.replace(",", "."))
        except ValueError:
            return op, value
    if isinstance(value, list):
        return op, tuple(sorted(map(str, value)))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return op, value
    if float(value).is_integer():
        value = int(value)
        if op in ("lt", "gt"):
            return ("lte", value - 1) if op == "lt" else ("gte", value + 1)
    return op, value


def _fits(alternative: str, item: Item) -> bool:
    """Подходит ли элемент разбора под вариант эталона: «N04», «N04 gte», «N04 lte 2»."""
    feature, *direction = alternative.split()
    if not direction:
        return feature in item.features
    op = direction[0]
    for f, c_op, c_value in item.conds:
        if f != feature:
            continue
        if len(direction) == 1:
            if _DIRECTION.get(c_op, c_op) == _DIRECTION.get(op, op):
                return True
        elif (c_op, c_value) == _cond(op, direction[1]):
            return True
    return False


def produced(ast: QueryAST) -> tuple[list[Item], list[Item]]:
    """Обязательные и желательные элементы разбора, сгруппированные по фрагменту запроса."""
    required_ooc_atoms = {
        o.atom_id for o in ast.out_of_catalog if o.requiredness == "required" and o.atom_id
    }
    hard: dict[Any, Item] = {}
    soft: dict[Any, Item] = {}

    def add(bucket: dict[Any, Item], span, features: set[str], atom_id, leftover=False) -> Item:
        key = (span.start, span.end) if span else id(features)
        item = bucket.setdefault(key, Item(key if span else None, set(), leftover))
        item.features |= features
        item.text = item.text or (span.text if span else "")
        if atom_id and atom_id not in item.atoms:
            item.atoms.append(atom_id)
        return item

    for c in ast.hard:
        item = add(hard, c.span, {c.feature_id}, c.atom_id)
        item.conds.add((c.feature_id, *_cond(c.op, c.value)))
    for c in ast.soft:
        if c.atom_id not in required_ooc_atoms:  # частичное обязательное уже учтено как ooc
            item = add(soft, c.span, {c.feature_id}, c.atom_id)
            item.conds.add((c.feature_id, *_cond(c.op, c.value)))
    for o in ast.out_of_catalog:
        bucket = hard if o.requiredness == "required" else soft
        add(bucket, o.span, {"ooc", *o.nearest_contracts}, o.atom_id, is_leftover(o))
    return list(hard.values()), list(soft.values())


def pairs(
    expected: list[list[str]], items: list[Item]
) -> tuple[list[tuple[list[str], Item | None]], list[Item]]:
    """Жадное сопоставление один к одному: (ожидание, найденный элемент или None), лишнее."""
    free = list(items)
    out = []
    for e in expected:
        found = next((it for it in free if any(_fits(a, it) for a in e)), None)
        if found is not None:
            free.remove(found)
        out.append((e, found))
    return out, free


def match(expected: list[list[str]], items: list[Item]) -> tuple[int, list[list[str]], list[Item]]:
    """Жадное сопоставление один к одному: (совпало, не найдено, лишнее)."""
    found, free = pairs(expected, items)
    return sum(it is not None for _, it in found), [e for e, it in found if it is None], free


CSV_FIELDS = [
    "query_id", "atom_id", "expected", "got", "requiredness", "got_features", "fragment", "note",
    "source",
]  # fmt: skip


def csv_rows(
    case: dict[str, Any], kind: str, expected: list[list[str]], items: list[Item], extra: list[Item]
) -> list[dict[str, str]]:
    """Строки для `tests/eval/metrics.py --parse`: совпадение — как в отчёте выше."""
    found, _ = pairs(expected, items)
    rows = []
    for i, (e, it) in enumerate(found, 1):
        label = "|".join(e)
        rows.append(
            {
                "query_id": case["id"],
                "atom_id": ",".join(it.atoms) if it else f"{kind}{i}",
                "expected": label,
                "got": label if it else "",
                "requiredness": kind,
                "got_features": "|".join(sorted(it.features)) if it else "",
                "fragment": it.text if it else "",
                "note": "" if it else "не найдено",
                "source": case.get("source", ""),
            }
        )
    for it in extra:
        rows.append(
            {
                "query_id": case["id"],
                "atom_id": ",".join(it.atoms),
                "expected": "",
                "got": "|".join(sorted(it.features)),
                "requiredness": kind,
                "got_features": "|".join(sorted(it.features)),
                "fragment": it.text,
                "note": "лишнее",
                "source": case.get("source", ""),
            }
        )
    return rows


def evaluate_case(
    case: dict[str, Any], mode: str, rows_out: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    outcome = parse_query(case["text"], case.get("city_id"), today=TODAY, mode=mode)
    elapsed = round((time.monotonic() - started) * 1000)
    ast = outcome.ast
    hard_items, soft_items = produced(ast)
    extra_ok = set(case.get("hard_extra_ok", []))
    h_tp, h_missed, h_extra = match(case["hard"], hard_items)
    # Непонятое рядом с «обязательно» — осторожность, а не придуманное условие: не «лишнее».
    leftovers = [it for it in h_extra if it.leftover]
    h_extra = [it for it in h_extra if not it.features & extra_ok and not it.leftover]
    s_tp, s_missed, s_extra = match(case["soft"], soft_items)
    # Обязательное не найдено среди обязательных: понижено до желательного или потеряно.
    _, lost, _ = match(h_missed, soft_items)
    downgraded = len(h_missed) - len(lost)
    if rows_out is not None:
        rows_out += csv_rows(case, "hard", case["hard"], hard_items, h_extra)
        rows_out += csv_rows(case, "soft", case["soft"], soft_items, s_extra)
    return {
        "id": case["id"],
        "parsed_by": ast.parsed_by,
        "notes": outcome.notes,
        "problems": outcome.problems,
        "ms": elapsed,
        "llm": f"{outcome.llm.provider} / {outcome.llm.model}" if outcome.llm else None,
        "usage": outcome.llm.usage if outcome.llm else {},
        "hard": {"tp": h_tp, "fn": len(h_missed), "fp": len(h_extra), "missed": h_missed},
        "soft": {"tp": s_tp, "fn": len(s_missed), "fp": len(s_extra), "missed": s_missed},
        "extra_hard": [sorted(it.features) for it in h_extra],
        "extra_soft": [sorted(it.features) for it in s_extra],
        "lost_required": lost,
        "required_leftovers": len(leftovers),
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
        # Провайдер и модель живого прогона — по ответам шлюза (у правил — пусто).
        "llm": sorted({r["llm"] for r in rows if r.get("llm")}),
        "parsed_by": {p: sum(r["parsed_by"] == p for r in rows) for p in ("rules", "llm")},
        "hard": tot["hard"] | _prf(**tot["hard"]),
        "soft": tot["soft"] | _prf(**tot["soft"]),
        "exact_hard_cases": sum(r["exact_hard"] for r in rows),
        "lost_required": sum(len(r["lost_required"]) for r in rows),
        "required_leftovers": sum(r.get("required_leftovers", 0) for r in rows),
        "downgraded_required": sum(r["downgraded_required"] for r in rows),
        "cases_with_unparsed": sum(bool(r["unparsed"]) for r in rows),
        "cases_with_problems": sum(bool(r["problems"]) for r in rows),
        "median_ms": sorted(r["ms"] for r in rows)[len(rows) // 2] if rows else None,
        "tokens": tokens,
    }


def _run_note(s: dict[str, Any]) -> str:
    """«, effort low, прогон 2026-10-27» — настройки живого прогона, если они записаны."""
    parts = [
        f"effort {s['llm_effort']}" if s.get("llm_effort") else "",
        f"прогон {s['run_date']}" if s.get("run_date") else "",
    ]
    return "".join(f", {p}" for p in parts if p)


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
        *(
            [f"Модель (провайдер / модель): {', '.join(s['llm'])}" + _run_note(s), ""]
            if s.get("llm")
            else []
        ),
        *([s["note"], ""] if s.get("note") else []),
        "| Метрика | Значение |",
        "|---|---|",
        f"| Обязательные: точность / полнота | {s['hard']['precision']} / {s['hard']['recall']} |",
        f"| Желательные: точность / полнота | {s['soft']['precision']} / {s['soft']['recall']} |",
        f"| Запросов с точным набором обязательных | {s['exact_hard_cases']} из {s['cases']} |",
        f"| **Потеряно обязательных** | **{s['lost_required']}** |",
        f"| Обязательных понижено до желательных | {s['downgraded_required']} |",
        f"| Непонятое рядом с «обязательно» → «уточнить» | {s['required_leftovers']} |",
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
    ap.add_argument("--csv", type=Path, help="строка на условие для tests/eval/metrics.py --parse")
    args = ap.parse_args(argv)
    cases = [json.loads(line) for line in REFERENCE.read_text("utf-8").splitlines() if line]
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids]
    atoms: list[dict[str, str]] = []
    rows = [evaluate_case(c, args.mode, atoms) for c in cases]
    summary = summarize(rows, args.mode)
    if args.mode != "rules":
        summary |= {"llm_effort": LLMSettings.from_env().effort, "run_date": str(date.today())}
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary, "cases": rows}
    (args.out / f"parse_{args.mode}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8"
    )
    (args.out / f"parse_{args.mode}.md").write_text(markdown(summary, rows), "utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8", newline="") as f:
            # LF на любой ОС: перевыгрузка на Mac и Windows не меняет весь файл.
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(atoms)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return report


if __name__ == "__main__":
    main()
