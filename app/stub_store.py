"""Заглушки API: настоящие факты v9 для отелей показа.

Файлы — app/fixtures, их собирает pipeline/make_fixtures.py.

Каждый факт при загрузке проходит контракт `core.facts.Fact` — так заглушки заодно проверяют,
что модель факта принимает данные v9. Интерфейс — `core.store.Store`, как у базы.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core import freshness, live
from core.facts import Fact
from core.hotelier import AnswerError, AnswerResult, placement_id, replaces_placements
from core.packs import stable_id
from core.placement_catalog import placement_choices, placement_maps, with_alias

FIXTURES = Path(__file__).parent / "fixtures"


def _listed(value: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Места отеля в заглушке: одно место или список, ближайшие — первыми."""
    places = value if isinstance(value, list) else [value] if value else []
    return sorted(places, key=lambda p: p["distance_m"])


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
    # Корпуса и фасады (строки пакета `hotel_buildings`, `buildings`, `facades`) — тоже из тестов.
    hotel_buildings: list[dict[str, Any]] = field(default_factory=list)
    buildings: list[dict[str, Any]] = field(default_factory=list)
    facades: list[dict[str, Any]] = field(default_factory=list)
    # Места рядом (D34): категория → отель → ближайшее место (или список мест с метками `tags`
    # для характеристик каталога «место рядом»); в заглушках мест нет — из тестов.
    places_near: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # Цели «доехать до X»: город → [{place_id, name, …, distances: {hotel_id: м}}] — из тестов.
    targets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Характеристики, включённые по описанию текстом, — в памяти до перезапуска.
    features_on: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Ответы отельера в режиме заглушек живут в памяти до перезапуска.
    superseded: dict[str, str] = field(default_factory=dict)
    fact_conflict_list: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)  # журнал «вне каталога»

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

    def freshness(self, city_id: str, as_of: datetime) -> list[dict[str, Any]]:
        return freshness.summary([f for f in self.facts.values() if f.city_id == city_id], as_of)

    def hotels(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [h for h in self.hotel_by_id.values() if not city_id or h["city_id"] == city_id]

    def hotel(self, hotel_id: str) -> dict[str, Any] | None:
        return self.hotel_by_id.get(hotel_id)

    def hotel_facts(self, hotel_id: str) -> dict[str, list[Fact]]:
        return dict(sorted(self.by_hotel.get(hotel_id, {}).items()))

    def fact(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id) or live.recent(fact_id)  # прогноз на запросе — в памяти

    def facts_for(
        self,
        feature_id: str,
        hotel_ids: Collection[str],
        params: dict[str, Any] | None = None,
        key_params: Collection[str] = (),
    ) -> dict[str, list[Fact]]:
        return {
            h: self.by_hotel[h][feature_id]
            for h in hotel_ids
            if feature_id in self.by_hotel.get(h, {})
        }

    def room_types(self, hotel_ids: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        return {h: self.room_types_by_hotel[h] for h in hotel_ids if h in self.room_types_by_hotel}

    def nearest_places(
        self,
        hotel_ids: Collection[str],
        category: str,
        radius_m: float,
        rank: Callable[[dict[str, Any]], int | None] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        known = self.places_near.get(category, {})
        if rank is None:
            near = {
                h: ps[0]
                for h in hotel_ids
                if (ps := [p for p in _listed(known.get(h)) if p["distance_m"] <= radius_m])
            }
            return near, len(known)
        # Как в базе: ранг места по меткам, лучшее по рангу, среди равных — ближайшее.
        ranked: dict[str, int] = {}
        near = {}
        for h, value in known.items():
            for p in _listed(value):
                r = rank({"city_id": self.hotel_by_id.get(h, {}).get("city_id")} | p)
                if r is None:
                    continue
                ranked[p["place_id"]] = r
                best = near.get(h)
                if (
                    h in hotel_ids
                    and p["distance_m"] <= radius_m
                    and (best is None or (r, p["distance_m"]) < (best["rank"], best["distance_m"]))
                ):
                    near[h] = p | {"rank": r, "tags": p.get("tags") or {}}
        return near, len(ranked)

    def place_counts(
        self, hotel_ids: Collection[str], category: str, radius_m: float
    ) -> tuple[dict[str, dict[str, Any]], int]:
        near, known = self.nearest_places(hotel_ids, category, radius_m)
        counts = {h: {"count": 0, "nearest_m": None} for h in hotel_ids}
        counts |= {h: {"count": 1, "nearest_m": p["distance_m"]} for h, p in near.items()}
        return counts, known

    def find_target(self, city_id: str, text: str) -> dict[str, Any] | None:
        from core.geo_store import target_match, target_rank, target_stems

        stems = target_stems(text)
        found = [
            t for t in self.targets.get(city_id, []) if stems and target_match(t["name"], stems)
        ]
        if not found:
            return None
        return min(found, key=lambda t: target_rank(t["name"], t.get("categories"), len(stems)))

    def distances(self, hotel_ids: Collection[str], target: dict[str, Any]) -> dict[str, int]:
        return {h: d for h, d in target.get("distances", {}).items() if h in hotel_ids}

    def save_feature(
        self,
        feature: dict[str, Any],
        spec_yaml: str,
        facts: list[Fact],
        cities: dict[str, Any],
        source_text: str | None,
    ) -> None:
        fid = feature["id"]
        for f in [f for f in self.facts.values() if f.feature_id == fid]:
            del self.facts[f.fact_id]
        for features in self.by_hotel.values():
            features.pop(fid, None)
        for f in facts:
            self.facts[f.fact_id] = f
            self.by_hotel.setdefault(f.hotel_id, {}).setdefault(fid, []).append(f)
        self.features_on[fid] = {**feature, "cities": cities, "yaml": spec_yaml}

    def feature_specs(self) -> list[dict[str, Any]]:
        return [v for _, v in sorted(self.features_on.items())]

    def placement_geometry(self, hotel_id: str) -> dict[str, Any]:
        """Подмножество `core.geo_store.GeoStore.geometry`, нужное агенту размещений."""
        links = [link for link in self.hotel_buildings if link["hotel_id"] == hotel_id]
        bids = {link["building_id"] for link in links}
        facades = [f for f in self.facades if f["building_id"] in bids]
        aliases, sides = placement_maps(links, facades, hotel_id)
        return {
            "hotel_buildings": links,
            "facades": facades,
            "placement_building_aliases": aliases,
            "placement_facades": [
                {"building_id": b, "side": side, "facade_id": fid}
                for (b, side), fid in sorted(sides.items())
            ],
            "placement_building_choices": placement_choices(
                links, [b for b in self.buildings if b["id"] in bids], facades, hotel_id
            ),
        }

    def add_building_alias(self, hotel_id: str, building_id: str, alias: dict[str, Any]) -> bool:
        for link in self.hotel_buildings:
            if link["hotel_id"] == hotel_id and link["building_id"] == building_id:
                link["evidence"] = with_alias(link.get("evidence"), alias)
                return True
        return False

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
            prev = (rt.get("placement_set") or {}).get("status")
            if replaces_placements(result.placement_status, prev):
                self._replace_placements(rt, result)
            else:  # ответ имитатора не заменяет набор настоящего отеля или команды (D41-8)
                result.placements_kept, result.placements = prev, None
        q = (
            self._question_by_id(result.question_id)
            if isinstance(result.question_id, int)
            else None
        )
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

    @staticmethod
    def _replace_placements(rt: dict[str, Any], result: AnswerResult) -> None:
        ps = result.placements  # не None: проверил apply_answer
        merged: dict[str, dict[str, Any]] = {}
        for item in ps.items:
            pid = placement_id(rt["room_type_id"], item)
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
        rt["placements"] = sorted(merged.values(), key=lambda p: (p["building_id"], p["floor"]))

    def fact_conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.fact_conflict_list if not city_id or c["city_id"] == city_id]

    def conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.ota_comparisons if not city_id or c["city_id"] == city_id]

    def record_unmatched(self, events: list[dict[str, Any]]) -> None:
        known = {e["event_id"] for e in self.unmatched}
        self.unmatched += [e for e in events if e["event_id"] not in known]

    def unmatched_events(
        self, city_id: str | None = None, stratum: str | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        return [
            e
            for e in self.unmatched
            if (not city_id or e["city_id"] == city_id)
            and (not stratum or e["source_stratum"] == stratum)
            and (not since or e["created_at"] >= since)
        ]
