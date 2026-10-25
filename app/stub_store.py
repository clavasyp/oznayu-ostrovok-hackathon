"""Заглушки API: настоящие факты v9 для отелей показа.

Файлы — app/fixtures, их собирает pipeline/make_fixtures.py.

Каждый факт при загрузке проходит контракт `core.facts.Fact` — так заглушки заодно проверяют,
что модель факта принимает данные v9. Интерфейс — `core.store.Store`, как у базы.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.facts import Fact
from core.hotelier import AnswerError, AnswerResult
from core.packs import stable_id

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class StubStore:
    stub = True
    hotel_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)
    by_hotel: dict[str, dict[str, list[Fact]]] = field(default_factory=dict)
    city_list: list[dict[str, Any]] = field(default_factory=list)
    coverage_by_city: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    questions_by_hotel: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    ota_comparisons: list[dict[str, Any]] = field(default_factory=list)
    # Категории с размещениями (D17) — в заглушках пока нет; тесты кладут сюда свои.
    room_types_by_hotel: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Ответы отельера в режиме заглушек живут в памяти до перезапуска.
    superseded: dict[str, str] = field(default_factory=dict)
    fact_conflict_list: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path = FIXTURES) -> StubStore:
        store = cls()
        store.hotel_by_id = {
            h["hotel_id"]: h for h in json.loads((root / "hotels.json").read_text("utf-8"))
        }
        by_hotel: dict[str, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))
        with gzip.open(root / "facts.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                fact = Fact.model_validate_json(line)
                store.facts[fact.fact_id] = fact
                by_hotel[fact.hotel_id][fact.feature_id].append(fact)
        store.by_hotel = {h: dict(v) for h, v in by_hotel.items()}
        store.city_list = json.loads((root / "cities.json").read_text("utf-8"))
        store.coverage_by_city = json.loads((root / "coverage.json").read_text("utf-8"))
        store.questions_by_hotel = json.loads((root / "questions.json").read_text("utf-8"))
        for items in store.questions_by_hotel.values():
            for q in items:  # тот же ID, что у вопроса в пакете (pipeline/build_v9_packs.py)
                q.setdefault("question_id", stable_id("question", q["hotel_id"], q["feature_id"]))
        store.ota_comparisons = json.loads((root / "conflicts.json").read_text("utf-8"))
        return store

    # Интерфейс core.store.Store

    def cities(self) -> list[dict[str, Any]]:
        return [c | {"hotels": c.get("hotels_in_sample", 0)} for c in self.city_list]

    def coverage(self, city_id: str) -> list[dict[str, Any]] | None:
        return self.coverage_by_city.get(city_id)

    def hotels(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [h for h in self.hotel_by_id.values() if not city_id or h["city_id"] == city_id]

    def hotel(self, hotel_id: str) -> dict[str, Any] | None:
        return self.hotel_by_id.get(hotel_id)

    def hotel_facts(self, hotel_id: str) -> dict[str, list[Fact]]:
        return dict(sorted(self.by_hotel.get(hotel_id, {}).items()))

    def fact(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def facts_for(self, feature_id: str, hotel_ids: Collection[str]) -> dict[str, list[Fact]]:
        return {
            h: self.by_hotel[h][feature_id]
            for h in hotel_ids
            if feature_id in self.by_hotel.get(h, {})
        }

    def room_types(self, hotel_ids: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        return {h: self.room_types_by_hotel[h] for h in hotel_ids if h in self.room_types_by_hotel}

    def questions(self, hotel_id: str) -> list[dict[str, Any]]:
        return self.questions_by_hotel.get(hotel_id, [])

    def question(
        self, hotel_id: str, question_id: int | None, feature_id: str | None
    ) -> dict[str, Any] | None:
        for q in self.questions_by_hotel.get(hotel_id, []):
            if (question_id is not None and q["question_id"] == question_id) or (
                question_id is None and q["feature_id"] == feature_id
            ):
                return q
        return None

    def _question_by_id(self, question_id: int) -> dict[str, Any] | None:
        for items in self.questions_by_hotel.values():
            for q in items:
                if q["question_id"] == question_id:
                    return q
        return None

    def set_question_state(self, question_id: int, state: str, at: datetime) -> None:
        q = self._question_by_id(question_id)
        if q is not None:
            q["answer_status"], q["asked_at"] = state, at.isoformat()

    def apply_answer(self, result: AnswerResult) -> None:
        if result.placements:
            rts = {
                rt["room_type_id"]: rt for rt in self.room_types_by_hotel.get(result.hotel_id, [])
            }
            rt = rts.get(result.room_type_id or "")
            if rt is None:
                raise AnswerError(
                    f"нет категории {result.room_type_id}: размещения не к чему привязать"
                )
            ps = result.placements
            merged: dict[str, dict[str, Any]] = {}
            for item in ps.items:
                pid = f"{rt['room_type_id']}|{item.building_id}|{item.floor}"
                p = merged.setdefault(
                    pid,
                    {
                        "placement_id": pid,
                        "building_id": item.building_id,
                        "floor": item.floor,
                        "floor_label": item.floor_label,
                        "windows": [],
                    },
                )
                p["windows"] = sorted(set(p["windows"]) | set(item.windows))
            rt["placement_set"] = {
                "completeness": ps.completeness,
                "guarantee": ps.guarantee,
                "status": result.placement_status,
                "source": result.placement_source,
            }
            rt["placements"] = list(merged.values())
        q = self._question_by_id(result.question_id)
        if q is not None:
            q.update(
                answer_status=result.state,
                answer_text=result.answer_text,
                answered_at=result.answered_at.isoformat(),
                parsed=result.parsed,
                is_simulated=result.is_simulated,
            )
        for f in result.facts:
            self.facts[f.fact_id] = f
            self.by_hotel.setdefault(f.hotel_id, {}).setdefault(f.feature_id, []).append(f)
        for old, new in result.superseded.items():
            self.superseded[old] = new
            fact = self.facts.get(old)
            if fact is not None:
                items = self.by_hotel.get(fact.hotel_id, {}).get(fact.feature_id, [])
                items[:] = [f for f in items if f.fact_id != old]
        for ours, theirs in result.conflicts:
            self.fact_conflict_list.append(
                {
                    "conflict_id": len(self.fact_conflict_list) + 1,
                    "city_id": theirs.city_id,
                    "hotel_id": theirs.hotel_id,
                    "feature_id": theirs.feature_id,
                    "subject_id": theirs.subject_id,
                    "fact_a": ours.fact_id,
                    "fact_b": theirs.fact_id,
                    "state": "detected",
                    "updated_at": result.answered_at.isoformat(),
                }
            )

    def fact_conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.fact_conflict_list if not city_id or c["city_id"] == city_id]

    def conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.ota_comparisons if not city_id or c["city_id"] == city_id]
