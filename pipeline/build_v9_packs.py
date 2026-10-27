"""Пакеты городов из расчётов v9 → packs/<city>/. Запуск: `python -m pipeline.build_v9_packs`.

Вход — research_notes/Расчёт характеристик (Codex v9, сверено): 439 отелей восьми городов,
35 015 фактов, вопросы отельеру с предзаполнением, покрытие и сверка с карточкой Островка.
Шесть автоматических городов (D36) идут в packs/ как есть. Москва и Сочи в packs/ — полный
каталог Островка (`pipeline.build_packs` → `finalize_packs` → `supplement_categories`), их v9
нужна только как вход `build_packs`: собирать в другую папку — `--out /tmp/v9`. Пакет другой
сборки сборщик v9 не перезаписывает.

Сборка детерминированная: те же входы → те же байты и те же sha256 в manifest.json.
"""

from __future__ import annotations

import ast
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any

from core.facts import Fact
from core.packs import (
    Manifest,
    PackError,
    PackFile,
    PackSource,
    keep_layers,
    move_provenance_params,
    sha256_file,
    stable_id,
    write_jsonl_gz,
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
V9 = ROOT / "research_notes" / "Расчёт характеристик"
PACKS = ROOT / "packs"
# .2: пути к файлам расчёта не в params, в паспортах — пути от корня репозитория
BUILDER_VERSION = "v9-packs.2"

INPUTS = {
    "facts": V9 / "integrated" / "all8_facts.jsonl.gz",
    "hotels": V9 / "cohort" / "hotels.jsonl",
    "questions": V9 / "hotelier" / "prefill.jsonl",
    "coverage": V9 / "integrated" / "all8_coverage.csv",
    "ota_comparisons": V9 / "qa" / "writer" / "ota_comparisons_all8.csv",
}
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
SLIM_KEYS = ("component_diagnostics", "audit_files", "components")  # тяжёлая диагностика v9


def hotel_row(h: dict[str, Any]) -> dict[str, Any]:
    try:
        sources = ast.literal_eval(h.get("sources") or "[]")  # в v9 — repr списка, не JSON
    except (ValueError, SyntaxError):
        sources = []
    return {
        "id": h["hotel_id"],
        "city_id": h["city_id"],
        "name": h["name"],
        "address": h.get("address"),
        "kind": h.get("hotel_kind"),
        "stars": None,
        # Для Сочи — населённый пункт из адреса страницы Островка (Сириус, Красная Поляна…).
        "subzone": h.get("source_city") if h["city_id"] == "sochi" else None,
        "latitude": float(h["latitude"]),
        "longitude": float(h["longitude"]),
        "source": {
            "url": h.get("hotel_url"),
            "json_pointer": h.get("json_pointer"),
            "ota_slug": h.get("ota_slug"),
            "cohort_origin": h.get("cohort_origin"),
            "providers": sorted({s.get("provider") for s in sources if isinstance(s, dict)}),
        },
    }


def fact_row(f: dict[str, Any]) -> dict[str, Any]:
    f = move_provenance_params({k: v for k, v in f.items() if k not in SLIM_KEYS})
    Fact.model_validate(f)  # пакет не соберётся, если факт не проходит контракт
    return f


def question_row(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": stable_id("question", q["hotel_id"], q["feature_id"]),
        "hotel_id": q["hotel_id"],
        "feature_id": q["feature_id"],
        "subject_level": "hotel",
        "subject_id": f"hotel:{q['hotel_id']}",
        "text": q["question"],
        "prefill": q.get("claims") or [],
        "fields": q.get("parsed_fields") or [],
        "priority": 0.0,
        "reason": "unknown",
        "state": q["answer_status"],  # как в v9: not_asked, …
        "answer_text": q.get("answer_text"),
        "is_simulated": bool(q.get("is_simulated")),
    }


def coverage_row(r: dict[str, str]) -> dict[str, Any]:
    return {
        "city_id": r["city_id"],
        "feature_id": r["feature_id"],
        "feature_name": r["feature_name"],
        "n_hotels": int(r["N_hotels"]),
        "computed": int(r["computed_n"]) + int(r["observed_n"]),
        "estimate": int(r["estimate_n"]),
        "unknown": int(r["unknown_n"]),
        "not_applicable": int(r["not_applicable_n"]),
    }


def ota_row(r: dict[str, str]) -> dict[str, Any]:
    try:
        other = json.loads(r["other_value"]) if r["other_value"] else None
    except json.JSONDecodeError:
        other = r["other_value"]
    return {
        "id": stable_id("ota", r["city_id"], r["hotel_id"], r["feature_id"], r["ota_pointer"]),
        "city_id": r["city_id"],
        "hotel_id": r["hotel_id"],
        "feature_id": r["feature_id"],
        "ota_value": r["ota_value"] or None,
        "other_value": other,
        "classification": r["classification"],
        "numeric_comparison": r["numeric_comparison"] or None,
        "resolution": r["resolution"] or None,
        "source": {"url": r["ota_url"], "json_pointer": r["ota_pointer"]},
    }


def pack_sources(facts: list[dict[str, Any]]) -> list[PackSource]:
    seen: dict[tuple[str, str | None], PackSource] = {}
    for f in facts:
        for s in f.get("sources") or []:
            if not isinstance(s, dict) or not s.get("provider"):
                continue
            key = (s["provider"], s.get("license"))
            if key not in seen:
                seen[key] = PackSource(
                    id=hashlib.sha256(repr(key).encode()).hexdigest()[:12],
                    provider=s["provider"],
                    license=s.get("license"),
                    release=s.get("dataset_version"),
                )
    seen[("ostrovok.ru", None)] = PackSource(
        id="ostrovok",
        provider="ostrovok.ru (страницы отелей, разрешение организаторов)",
        url="https://ostrovok.ru",
    )
    return sorted(seen.values(), key=lambda s: (s.provider, s.license or ""))


def load_inputs() -> dict[str, Any]:
    with gzip.open(INPUTS["facts"], "rt", encoding="utf-8") as f:
        facts = [json.loads(line) for line in f if line.strip()]
    hotels = [json.loads(line) for line in INPUTS["hotels"].read_text("utf-8").splitlines()]
    questions = [json.loads(line) for line in INPUTS["questions"].read_text("utf-8").splitlines()]
    with INPUTS["coverage"].open(encoding="utf-8") as f:
        coverage = list(csv.DictReader(f))
    with INPUTS["ota_comparisons"].open(encoding="utf-8") as f:
        ota = list(csv.DictReader(f))
    return {"facts": facts, "hotels": hotels, "questions": questions, "coverage": coverage,
            "ota_comparisons": ota}  # fmt: skip


def foreign_packs(out_root: Path, cities: Collection[str]) -> list[str]:
    """Пакеты не сборки v9 в папке результата: их сборщик v9 не перезаписывает."""
    foreign = []
    for city in sorted(cities):
        path = out_root / city / "manifest.json"
        if path.is_file():
            code = json.loads(path.read_text("utf-8")).get("code_version") or ""
            if not code.startswith("v9-packs"):
                foreign.append(f"{city} ({code or 'без code_version'})")
    return foreign


def build(out_root: Path = PACKS, cities: Collection[str] | None = None) -> list[str]:
    data = load_inputs()
    inputs_sha = hashlib.sha256(
        (BUILDER_VERSION + "".join(sha256_file(p) for p in INPUTS.values())).encode()
    ).hexdigest()
    pack_version = f"v9-{inputs_sha[:10]}"
    by_city: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for h in data["hotels"]:
        by_city[h["city_id"]]["hotels"].append(hotel_row(h))
    for f in data["facts"]:
        by_city[f["city_id"]]["facts"].append(fact_row(f))
    for q in data["questions"]:
        by_city[q["city_id"]]["questions"].append(question_row(q))
    for r in data["coverage"]:
        by_city[r["city_id"]]["coverage"].append(coverage_row(r))
    for r in data["ota_comparisons"]:
        by_city[r["city_id"]]["ota_comparisons"].append(ota_row(r))

    sort_keys = {
        "hotels": lambda r: r["id"],
        "facts": lambda r: (r["hotel_id"], r["feature_id"], r["fact_id"]),
        "questions": lambda r: (r["hotel_id"], r["feature_id"]),
        "coverage": lambda r: r["feature_id"],
        "ota_comparisons": lambda r: (r["hotel_id"], r["feature_id"], r["id"]),
    }
    timezones = defaultdict(Counter)
    for h in data["hotels"]:
        timezones[h["city_id"]][h.get("timezone") or "UTC"] += 1

    if cities is not None:
        unknown = set(cities) - set(by_city)
        if unknown:
            raise PackError(f"нет таких городов в расчётах v9: {sorted(unknown)}")
        by_city = {c: parts for c, parts in by_city.items() if c in cities}
    foreign = foreign_packs(out_root, by_city)
    if foreign:
        raise PackError(
            f"{out_root}: пакеты другой сборки — {', '.join(foreign)}; сборщик v9 их не "
            "перезаписывает. Москва и Сочи в packs/ — полный каталог Островка. v9 как вход "
            "build_packs — в другую папку: --out /tmp/v9; шесть автоматических городов — "
            "--city spb --city kazan …"
        )

    report = []
    for city, parts in sorted(by_city.items()):
        pack = out_root / city
        (pack / "data").mkdir(parents=True, exist_ok=True)
        files = []
        for kind in ("hotels", "facts", "questions", "coverage", "ota_comparisons"):
            rows = sorted(parts.get(kind, []), key=sort_keys[kind])
            if not rows:
                continue
            path = pack / "data" / f"{kind}.jsonl.gz"
            write_jsonl_gz(path, rows)
            files.append(
                PackFile(
                    path=f"data/{kind}.jsonl.gz",
                    kind=kind,
                    rows=len(rows),
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
        calc = [f.get("calculated_at") for f in parts["facts"] if f.get("calculated_at")]
        manifest = Manifest(
            city_id=city,
            city_name=CITY_NAMES.get(city, city),
            timezone=timezones[city].most_common(1)[0][0],
            depth="full" if city in FULL_DEPTH else "automatic",
            pack_version=pack_version,
            built_at=datetime.fromisoformat(max(calc)) if calc else datetime(2026, 10, 24),
            code_version=BUILDER_VERSION,  # не git sha: пересборка без изменений не меняет пакет
            catalog_version="v9",
            sources=pack_sources(parts["facts"]),
            files=files,
            notes=(
                f"Сборка {BUILDER_VERSION} из research_notes/Расчёт характеристик (Codex v9). "
                "Диагностика v9 (component_diagnostics, audit_files, components) не включена."
            ),
        )
        manifest = keep_layers(pack, manifest)  # слои калькуляторов (вид и солнце) — как были
        write_manifest(pack, manifest)
        size = sum(f.bytes for f in files)
        report.append(
            f"{city}: " + ", ".join(f"{f.kind} {f.rows}" for f in files) + f"; {size / 1e6:.1f} МБ"
        )
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=PACKS, help="папка результата (packs/)")
    parser.add_argument("--city", action="append", help="только эти города (можно несколько)")
    args = parser.parse_args(argv)
    for line in build(args.out, args.city):
        print(line)


if __name__ == "__main__":
    main()
