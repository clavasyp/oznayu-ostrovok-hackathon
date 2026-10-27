"""Вопросы отельеру для всего каталога: шаблоны v9 и предзаполнение из заявлений Островка.

Когорта v9 пришла с вопросами и предзаполнением v9. Полный каталог Островка вопросов не имел,
и цикл отельера («спросить → ответ → пересчёт») для этих отелей отвечал 404. Здесь — те же
вопросы уровня отеля по шаблонам `agents/hotelier/questions_26.csv`:
- **новый отель** получает вопрос по каждой характеристике шаблона, если её не закрывает
  пригодный для обязательных условий факт уровня отеля с конкретным значением (явный запрет
  питомцев закрывает P01, «с животными можно, вид не назван» — нет);
- **предзаполнение** — заявления страницы Островка из фактов D26 (паспорт: адрес страницы,
  JSON pointer, хеш, дата). Это заявление, а не проверка: вопрос задаётся всё равно;
- **вопросы когорты** не заменяются: к их предзаполнению добавляются только заявления
  Островка с новой цитатой (например, после дополнения категорий из снимка 27.10).

Ответ на вопрос уровня отеля относится ко всем номерам (core/search.py, `HOTEL_WIDE`).
"""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from agents.hotelier.questions import QUESTIONS_CSV
from core.facts import Fact, Status
from core.packs import stable_id

VERSION = "hotelier-questions.1"
PRESENT = "present"  # «есть, тип не назван» (D39): вопрос нужен


def templates() -> dict[str, dict[str, str]]:
    with QUESTIONS_CSV.open(encoding="utf-8") as f:
        return {r["feature_id"]: r for r in csv.DictReader(f)}


def _claim(fact: Fact, quote: str, source: dict[str, Any], subjects: list[str]) -> dict:
    """Заявление в формате предзаполнения v9 (`quote`, `json_pointer`, `source_url` …)."""
    room = fact.subject_level.value == "room_type"
    digest = hashlib.sha256(f"{fact.fact_id}|{source.get('json_pointer')}".encode()).hexdigest()
    return {
        "atom_id": f"{fact.feature_id}_ostrovok_site",
        "claim_id": "ost_" + digest[:20],
        "fetched_at": source.get("fetched_at"),
        "full_contract_eligible": False,
        "hard_condition_eligible": bool(fact.usable_in_hard),
        "json_pointer": source.get("json_pointer"),
        "origin": fact.algorithm_version,
        "quote": quote,
        "scope_limitation": fact.scope_note,
        "source_hash": source.get("dataset_sha256"),
        "source_subject_id": subjects[0],
        "source_subject_ids": subjects,
        "source_url": source.get("url"),
        "status": fact.status.value,
        "subject_type": "room_group" if room else "hotel",
        "value": fact.value,
    }


def ostrovok_claims(facts: Iterable[Fact]) -> dict[tuple[str, str], list[dict]]:
    """(отель, характеристика) → заявления Островка: одно на цитату, все категории с ней —
    в `source_subject_ids`. Порядок детерминированный: уровень отеля, затем категории."""
    by_quote: dict[tuple[str, str], dict[str, tuple[Fact, dict, list[str]]]] = defaultdict(dict)
    ordered = sorted(
        (f for f in facts if f.status == Status.ostrovok_content),
        key=lambda f: (f.subject_level.value != "hotel", f.subject_id, f.fact_id),
    )
    for fact in ordered:
        subject = fact.subject_id.removeprefix("hotel:")
        for source in fact.sources:
            raw = source.model_dump(exclude_none=True)
            quote = raw.get("source_value")
            if not isinstance(quote, str) or not quote.strip():
                continue
            seen = by_quote[(fact.hotel_id, fact.feature_id)]
            if quote in seen:
                if subject not in seen[quote][2]:
                    seen[quote][2].append(subject)
                continue
            seen[quote] = (fact, raw, [subject])
    return {
        key: [_claim(fact, quote, raw, subjects) for quote, (fact, raw, subjects) in quotes.items()]
        for key, quotes in by_quote.items()
    }


def _specific(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and PRESENT not in value.values()
    return value is not None and value != PRESENT


def closed(facts: Iterable[Fact]) -> bool:
    """Вопрос не нужен: факт уровня отеля закрывает обязательное условие конкретным значением."""
    return any(
        f.subject_level.value == "hotel" and f.usable_in_hard and _specific(f.value) for f in facts
    )


def question_row(hotel_id: str, template: dict[str, str], claims: list[dict]) -> dict:
    """Строка `questions` пакета — как у когорты v9 (`pipeline.build_v9_packs.question_row`)."""
    return {
        "id": stable_id("question", hotel_id, template["feature_id"]),
        "hotel_id": hotel_id,
        "feature_id": template["feature_id"],
        "subject_level": "hotel",
        "subject_id": f"hotel:{hotel_id}",
        "text": template["free_text_question"],
        "prefill": claims,
        "fields": template["parsed_fields"].split(","),
        "priority": 0.0,
        "reason": "unknown",
        "state": "not_asked",
        "answer_text": None,
        "is_simulated": False,
    }


def _quote(claim: dict) -> str:
    return " ".join(str(claim.get("quote") or "").split()).strip('"«»')


def build_questions(
    hotels: Iterable[dict], facts: Iterable[dict | Fact], existing: Iterable[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Вопросы города: прежние (с дополненным предзаполнением) и новые по шаблонам."""
    shapes = templates()
    by_key: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for raw in facts:
        if (raw.feature_id if isinstance(raw, Fact) else raw["feature_id"]) not in shapes:
            continue
        fact = raw if isinstance(raw, Fact) else Fact.model_validate(raw)
        by_key[(fact.hotel_id, fact.feature_id)].append(fact)
    claims = ostrovok_claims(f for fs in by_key.values() for f in fs)
    stats = {"kept": 0, "prefill_extended": 0, "new": 0, "closed_by_fact": 0}
    rows, asked = [], set()
    for q in existing:
        key = (q["hotel_id"], q["feature_id"])
        asked.add(key)
        quotes = {_quote(c) for c in q.get("prefill") or []}
        extra = [c for c in claims.get(key, []) if _quote(c) not in quotes]
        if extra:
            q = q | {"prefill": list(q.get("prefill") or []) + extra}
            stats["prefill_extended"] += 1
        stats["kept"] += 1
        rows.append(q)
    for hotel in hotels:
        hid = hotel["id"]
        if any((hid, fid) in asked for fid in shapes):
            continue  # у отеля когорты свой набор вопросов v9
        for fid, template in sorted(shapes.items()):
            if closed(by_key.get((hid, fid), [])):
                stats["closed_by_fact"] += 1
                continue
            rows.append(question_row(hid, template, claims.get((hid, fid), [])))
            stats["new"] += 1
    rows.sort(key=lambda r: str(r["id"]))  # как write_pack: по строке id
    return rows, stats
