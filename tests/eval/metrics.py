"""Метрики качества: покрытие, «неизвестно», вопросы отельеру, точность на своей разметке.

Никаких «точность 100% от проверок формата»: каждая цифра здесь — либо измерение на фактах
с явным знаменателем, либо измерение на **своей размеченной выборке** (`annotations.csv`),
где у каждого случая есть эталон, ссылка-доказательство и дата.

Запуск из корня репозитория (факты и вопросы — из пакета города, как у жюри):

    python tests/eval/metrics.py --city sochi --annotations tests/eval/annotations.csv \
        --distances tests/eval/distances.csv --out tests/eval/baseline.md

Пакет читается по манифесту: **все** файлы `kind: facts` (`facts.jsonl.gz` и слои калькуляторов,
например `view_sun.jsonl.gz`) и `kind: questions` — так же, как загрузка в базу. `--facts` и
`--questions` нужны только для чужих файлов; `app/fixtures/questions.json` — заглушка показа,
для метрик не годится (из неё выходило «вопрос нужен 11 из 89»).

Что считается:

| Метрика | Знаменатель | Что значит |
|---|---|---|
| покрытие | отели города × характеристика | доля отелей с любым значением, вкл. оценки |
| покрытие без оценок | то же | доля отелей со значением, которое может закрыть обязательное (D18) |
| доля «неизвестно» | строки фактов | где мы честно не знаем (не «нет») |
| доля оценок | строки фактов | значения ниже порога публикации (D18) |
| доля вопросов отельеру | отели города | где без отельера не обойтись |
| точность | случаи со эталоном «да/нет» | верно / (верно + неверно) |
| ошибка в метрах | пары «наш путь — эталонный путь» | MAE, медиана (модулей), p90 |
| разбор запросов | условия запроса | точность и полнота по коду характеристики (не по порогу) |
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

VALUELESS = frozenset({"unknown", "not_applicable"})
ESTIMATE = "estimate"
HOTELIER = frozenset({"hotelier", "simulated_hotelier"})
VERDICTS = {"верно": "correct", "неверно": "wrong", "не проверить": "unverifiable"}


def pack_files(city: str | None, kind: str, root: Path = ROOT) -> list[Path]:
    """Файлы пакета города данного вида (`facts`, `questions`) — по манифесту, все, как при
    загрузке в базу (`pipeline/load_packs.py`): у Сочи файлов фактов два, второй — слой «вид и
    солнце». Это те же цифры, что видит жюри."""
    if not city:
        return []
    pack = root / "packs" / city
    manifest = pack / "manifest.json"
    if not manifest.is_file():
        return []
    files = json.loads(manifest.read_text("utf-8")).get("files", [])
    return [
        pack / f["path"] for f in files if f.get("kind") == kind and (pack / f["path"]).is_file()
    ]


def load_questions(path: Path, city: str | None) -> dict[str, list[dict[str, Any]]]:
    """Вопросы отельеру: пакет (JSONL/JSONL.gz, строка = вопрос) или заглушка (JSON по отелям)."""
    if path.suffix == ".json":
        payload = json.loads(path.read_text("utf-8"))
        if isinstance(payload, dict):
            return payload
        rows = payload
    else:
        rows = list(iter_facts(path))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        row.setdefault("city_id", city)
        grouped[str(row.get("hotel_id"))].append(row)
    return grouped


def iter_facts(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def coverage(facts: Iterable[dict[str, Any]], city: str | None = None) -> dict[str, dict]:
    """Покрытие по характеристикам: отель с известным значением / все отели с фактом.

    Два покрытия: «известно» считает и оценки (D18), «без оценок» — только значения, которые
    могут закрыть обязательное условие. У B02 первое 100%, второе 0%: там одни оценки."""
    hotels: dict[str, set[str]] = defaultdict(set)
    known: dict[str, set[str]] = defaultdict(set)
    reliable: dict[str, set[str]] = defaultdict(set)
    statuses: dict[str, Counter] = defaultdict(Counter)
    rows: Counter = Counter()
    for fact in facts:
        if city and fact.get("city_id") != city:
            continue
        feature = fact["feature_id"]
        hotels[feature].add(fact["hotel_id"])
        statuses[feature][fact["status"]] += 1
        rows[feature] += 1
        if fact["status"] not in VALUELESS:
            known[feature].add(fact["hotel_id"])
            if fact["status"] != ESTIMATE:
                reliable[feature].add(fact["hotel_id"])
    table = {}
    for feature, all_hotels in hotels.items():
        n = len(all_hotels)
        counts = statuses[feature]
        # У параметризованных характеристик (месяц, цель маршрута) строк больше, чем отелей:
        # доли статусов считаем по строкам, покрытие — по отелям.
        table[feature] = {
            "hotels": n,
            "rows": rows[feature],
            "known": len(known[feature]),
            "coverage": len(known[feature]) / n if n else 0.0,
            "known_without_estimates": len(reliable[feature]),
            "coverage_without_estimates": len(reliable[feature]) / n if n else 0.0,
            "unknown": counts["unknown"],
            "not_applicable": counts["not_applicable"],
            "estimate": counts[ESTIMATE],
            "unknown_share": counts["unknown"] / rows[feature] if rows[feature] else 0.0,
            "estimate_share": counts[ESTIMATE] / rows[feature] if rows[feature] else 0.0,
            "statuses": dict(counts),
        }
    return dict(sorted(table.items()))


def hotelier(facts: Iterable[dict[str, Any]], questions: dict[str, list[dict]], city: str | None):
    """Сколько отелей без вопроса отельеру не обойтись и какие характеристики он закрывает."""
    by_city: dict[str, set[str]] = defaultdict(set)
    answered: dict[str, set[str]] = defaultdict(set)
    features: set[str] = set()
    for hotel_id, items in questions.items():
        for item in items:
            if city and item.get("city_id") != city:
                continue
            by_city[item.get("city_id", "")].add(hotel_id)
            features.add(item["feature_id"])
            if item.get("answer_text"):
                answered[item.get("city_id", "")].add(hotel_id)
    hotel_facts: dict[str, set[str]] = defaultdict(set)
    hotelier_facts = 0
    total_facts = 0
    for fact in facts:
        if city and fact.get("city_id") != city:
            continue
        total_facts += 1
        hotel_facts[fact["city_id"]].add(fact["hotel_id"])
        if fact["status"] in HOTELIER:
            hotelier_facts += 1
    cities = {city} if city else set(hotel_facts)
    return {
        "features_with_questions": sorted(features),
        "questions_total": sum(
            1 for items in questions.values() for i in items if not city or i.get("city_id") == city
        ),
        "questions_answered": sum(
            1
            for items in questions.values()
            for i in items
            if i.get("answer_text") and (not city or i.get("city_id") == city)
        ),
        "by_city": {
            c: {
                "hotels": len(hotel_facts.get(c, ())),
                "hotels_with_question": len(by_city.get(c, ())),
                "hotels_with_question_share": (
                    len(by_city.get(c, ())) / len(hotel_facts[c]) if hotel_facts.get(c) else 0.0
                ),
                "hotels_answered": len(answered.get(c, ())),
            }
            for c in sorted(cities)
        },
        "fact_rows_from_hotelier_share": hotelier_facts / total_facts if total_facts else 0.0,
    }


def checked_by_ai(row: dict[str, str]) -> bool:
    """Вердикт сделал ИИ-ассистент, человек его принял (D33, D41): «проверил ИИ-ассистент…»."""
    return "ИИ" in (row.get("checked_by") or "")


def accuracy(path: Path) -> dict[str, Any]:
    """Точность на своей разметке. Считается только по заполненным вердиктам.

    Минуты — только у строк, проверенных человеком: время работы ассистента — не трудозатраты
    разметки."""
    rows = list(csv.DictReader(path.open("r", encoding="utf-8")))
    verdicts = Counter()
    per_feature: dict[str, Counter] = defaultdict(Counter)
    minutes: list[float] = []
    by_ai = 0
    for row in rows:
        raw = (row.get("verdict") or "").strip().lower()
        if not raw:
            continue
        verdict = VERDICTS.get(raw, raw)
        verdicts[verdict] += 1
        per_feature[row.get("feature_id", "")][verdict] += 1
        if checked_by_ai(row):
            by_ai += 1
            continue
        try:
            minutes.append(float(row.get("minutes_spent") or ""))
        except ValueError:
            pass
    scored = verdicts["correct"] + verdicts["wrong"]
    return {
        "cases_total": len(rows),
        "cases_scored": scored,
        "correct": verdicts["correct"],
        "wrong": verdicts["wrong"],
        "unverifiable": verdicts["unverifiable"],
        "accuracy": verdicts["correct"] / scored if scored else None,
        "unverifiable_share": verdicts["unverifiable"] / len(rows) if rows else None,
        "checked_by_ai": by_ai,
        "per_feature": {
            f: {
                "correct": c["correct"],
                "wrong": c["wrong"],
                "unverifiable": c["unverifiable"],
                "accuracy": (
                    c["correct"] / (c["correct"] + c["wrong"])
                    if c["correct"] + c["wrong"]
                    else None
                ),
            }
            for f, c in sorted(per_feature.items())
        },
        "minutes_total": round(sum(minutes), 1) if minutes else None,
        "minutes_median": round(statistics.median(minutes), 1) if minutes else None,
    }


def distance_error(path: Path) -> dict[str, Any]:
    """Ошибка в метрах там, где есть эталонный путь: MAE, медиана и p90 модулей, а также
    медиана разности со знаком («наш − эталон»): это разные числа, на слайде — с подписью."""
    signed = []
    for row in csv.DictReader(path.open("r", encoding="utf-8")):
        try:
            signed.append(float(row["our_m"]) - float(row["reference_m"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not signed:
        return {"n": 0}
    errors = sorted(abs(d) for d in signed)
    p90 = errors[min(len(errors) - 1, int(round(0.9 * (len(errors) - 1))))]
    return {
        "n": len(errors),
        "mae_m": round(statistics.mean(errors), 1),
        "median_m": round(statistics.median(errors), 1),
        "p90_m": round(p90, 1),
        "max_m": round(max(errors), 1),
        "median_signed_m": round(statistics.median(signed), 1),
        "ours_longer": sum(1 for d in signed if d > 0),
    }


def parse_accuracy(path: Path) -> dict[str, Any]:
    """Разбор запроса: точность и полнота, отдельно обязательных и желательных условий.

    Строка CSV (`agents.eval.parse_eval --csv`) — условие: `expected` и `got` оба есть и равны —
    найдено; есть только `expected` — пропущено; только `got` — лишнее; оба, но разные — и то и
    другое. Точность = найдено / (найдено + лишнее), полнота = найдено / (найдено + пропущено).
    Сверяется **только код характеристики**: оператор, порог и отрицание — нет, поэтому это не
    «точность понимания запроса»."""
    per_kind: dict[str, Counter] = defaultdict(Counter)
    for row in csv.DictReader(path.open("r", encoding="utf-8")):
        kind = (row.get("requiredness") or "hard").strip().lower()
        expected = (row.get("expected") or "").strip()
        got = (row.get("got") or "").strip()
        if expected and expected == got:
            per_kind[kind]["found"] += 1
            continue
        if expected:
            per_kind[kind]["missed"] += 1
        if got:
            per_kind[kind]["extra"] += 1
    out = {}
    for kind, c in sorted(per_kind.items()):
        found, missed, extra = c["found"], c["missed"], c["extra"]
        out[kind] = {
            "expected": found + missed,
            "found": found,
            "missed": missed,
            "extra": extra,
            "precision": found / (found + extra) if found + extra else None,
            "recall": found / (found + missed) if found + missed else None,
        }
    return out


def markdown(
    cov: dict[str, dict],
    hotel: dict[str, Any],
    acc: dict[str, Any] | None,
    dist: dict[str, Any] | None,
    parse: dict[str, Any] | None,
    city: str | None,
) -> str:
    lines = [f"# Метрики качества{f' — {city}' if city else ''}", ""]
    if not cov:
        lines.append(
            "Факты не заданы (нет `--facts` и пакета города): покрытие и вопросы не считались."
        )
    else:
        lines += [
            "## Покрытие и «неизвестно»",
            "",
            "«Покрытие» считает и оценки (D18); обязательное условие закрывает только значение "
            "без оценки — колонка «Без оценок».",
            "",
            "| Характеристика | Отелей | Известно | Покрытие | Без оценок | «Неизвестно» "
            "| Оценки |",
            "|---|---|---|---|---|---|---|",
        ]
        for feature, row in cov.items():
            lines.append(
                f"| {feature} | {row['hotels']} | {row['known']} | {row['coverage']:.0%} | "
                f"{row['coverage_without_estimates']:.0%} | "
                f"{row['unknown_share']:.0%} | {row['estimate_share']:.0%} |"
            )
    if hotel:
        lines += ["", "## Вопросы отельеру", ""]
        for c, row in hotel["by_city"].items():
            lines.append(
                f"- {c}: вопрос нужен {row['hotels_with_question']} из {row['hotels']} отелей "
                f"({row['hotels_with_question_share']:.0%}), отвечено {row['hotels_answered']}"
            )
        features = ", ".join(hotel["features_with_questions"]) or "—"
        lines.append(f"- вопросы покрывают характеристики: {features}")
        total, answered = hotel["questions_total"], hotel["questions_answered"]
        lines.append(f"- вопросов всего: {total}, с ответом: {answered}")
    if acc is not None:
        lines += ["", "## Точность на своей разметке", ""]
        if acc["accuracy"] is None:
            if acc["unverifiable"]:
                lines.append(
                    f"Точность не измерена: размечено {acc['unverifiable']} из "
                    f"{acc['cases_total']} случаев, и все они — «не проверить»: эталон "
                    "описывает другой смысл, сравнивать значения не с чем."
                )
            else:
                lines.append(
                    f"Не размечено: {acc['cases_total']} случаев в таблице, вердиктов нет. "
                    "Заполните `verdict` в `tests/eval/annotations.csv`."
                )
        else:
            lines.append(
                f"Точность {acc['accuracy']:.0%} на {acc['cases_scored']} проверенных случаях "
                f"(верно {acc['correct']}, неверно {acc['wrong']}); "
                f"«не проверить» — {acc['unverifiable']} из {acc['cases_total']}."
            )
            if acc["minutes_median"] is not None:
                lines.append(
                    f"Время: всего {acc['minutes_total']} мин, "
                    f"медиана {acc['minutes_median']} мин на случай."
                )
            lines += [
                "",
                "| Характеристика | Верно | Неверно | Не проверить | Точность |",
                "|---|---|---|---|---|",
            ]
            for feature, row in acc["per_feature"].items():
                value = "—" if row["accuracy"] is None else f"{row['accuracy']:.0%}"
                lines.append(
                    f"| {feature} | {row['correct']} | {row['wrong']} | "
                    f"{row['unverifiable']} | {value} |"
                )
        if acc["checked_by_ai"]:
            lines.append(
                f"Из размеченных случаев {acc['checked_by_ai']} проверил ИИ-ассистент, человек "
                "принял его вердикт (`checked_by`, D33): это не проверка человеком."
            )
    if dist and dist.get("n"):
        lines += [
            "",
            "## Ошибка в метрах",
            "",
            f"MAE {dist['mae_m']} м, медиана модулей {dist['median_m']} м, p90 {dist['p90_m']} м, "
            f"максимум {dist['max_m']} м на {dist['n']} парах с эталонным путём. Разность "
            f"«наш − эталон»: медиана {dist['median_signed_m']:+} м, наш длиннее в "
            f"{dist['ours_longer']} из {dist['n']}.",
        ]
    if parse:
        lines += [
            "",
            "## Разбор запросов",
            "",
            "Сверяется только код характеристики: оператор, порог и отрицание — нет. Это "
            "«условие найдено», а не точность понимания запроса.",
            "",
        ]
        for kind, row in parse.items():
            precision = "—" if row["precision"] is None else f"{row['precision']:.0%}"
            recall = "—" if row["recall"] is None else f"{row['recall']:.0%}"
            lines.append(
                f"- {kind}: точность {precision}, полнота {recall} — найдено {row['found']} "
                f"из {row['expected']} ожидаемых, лишних {row['extra']}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--facts", type=Path, action="append", help="JSONL(.gz) с фактами; можно несколько"
    )
    parser.add_argument("--city", help="город (по умолчанию все)")
    parser.add_argument(
        "--questions", type=Path, action="append", help="вопросы отельеру; можно несколько"
    )
    parser.add_argument("--annotations", type=Path, help="CSV своей разметки")
    parser.add_argument("--distances", type=Path, help="CSV с our_m и reference_m")
    parser.add_argument("--parse", type=Path, help="CSV разбора запросов: expected/got")
    parser.add_argument("--out", type=Path, help="куда записать markdown")
    parser.add_argument("--json", type=Path, help="куда записать JSON со всеми числами")
    args = parser.parse_args()

    facts_files = args.facts or pack_files(args.city, "facts")
    questions_files = args.questions or pack_files(args.city, "questions")
    if not facts_files:
        print("# нет --facts и нет пакета города: считаю только разметку и расстояния\n")

    facts = [fact for path in facts_files for fact in iter_facts(path)]
    cov = coverage(facts, args.city) if facts else {}
    questions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in (p for p in questions_files if p.is_file()):
        for hotel_id, items in load_questions(path, args.city).items():
            questions[hotel_id] += items
    hotel = hotelier(facts, questions, args.city) if facts else {}
    acc = accuracy(args.annotations) if args.annotations and args.annotations.is_file() else None
    dist = distance_error(args.distances) if args.distances and args.distances.is_file() else None
    parse = parse_accuracy(args.parse) if args.parse and args.parse.is_file() else None

    report = markdown(cov, hotel, acc, dist, parse, args.city)
    print(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, "utf-8")
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "coverage": cov,
                    "hotelier": hotel,
                    "accuracy": acc,
                    "distance": dist,
                    "parse": parse,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            "utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
