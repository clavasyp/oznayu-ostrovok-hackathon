"""Заглушки API: настоящие факты v9 для отелей показа (app/fixtures, собирает pipeline/make_fixtures.py).

Каждый факт при загрузке проходит контракт `core.facts.Fact` — так заглушки заодно проверяют,
что модель факта принимает данные v9.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.facts import Fact

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class StubStore:
    hotels: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)
    by_hotel: dict[str, dict[str, list[Fact]]] = field(default_factory=dict)
    cities: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    questions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path = FIXTURES) -> StubStore:
        store = cls()
        store.hotels = {
            h["hotel_id"]: h for h in json.loads((root / "hotels.json").read_text("utf-8"))
        }
        by_hotel: dict[str, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))
        with gzip.open(root / "facts.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                fact = Fact.model_validate_json(line)
                store.facts[fact.fact_id] = fact
                by_hotel[fact.hotel_id][fact.feature_id].append(fact)
        store.by_hotel = {h: dict(v) for h, v in by_hotel.items()}
        store.cities = json.loads((root / "cities.json").read_text("utf-8"))
        store.coverage = json.loads((root / "coverage.json").read_text("utf-8"))
        store.questions = json.loads((root / "questions.json").read_text("utf-8"))
        store.conflicts = json.loads((root / "conflicts.json").read_text("utf-8"))
        return store
