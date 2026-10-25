"""Хранилище на PostgreSQL + PostGIS (`DATA_MODE=db`): те же ответы, что у заглушек, но из базы,
куда `init` загрузил пакеты городов.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from core.db import (
    City,
    CityCoverage,
    FactRow,
    Hotel,
    OtaComparison,
    Placement,
    PlacementSet,
    PlacementWindow,
    Question,
    RoomType,
)
from core.facts import Fact

FACT_COLUMNS = [c.name for c in FactRow.__table__.columns]
# Служебные колонки таблицы, которых нет в контракте факта.
NOT_IN_FACT = {
    "confidence_level",
    "confidence_reason",
    "extra",
    "is_current",
    "superseded_by",
    "feature_version",
}


def fact_from_row(row: FactRow) -> Fact:
    """Строка таблицы `fact` → контракт `Fact`: обратное к `pipeline.load_packs.fact_row`."""
    data: dict[str, Any] = {c: getattr(row, c) for c in FACT_COLUMNS if c not in NOT_IN_FACT}
    extra = dict(row.extra or {})
    extra.pop("pack", None)
    confidence = extra.pop("confidence", {})
    if row.confidence_level or row.confidence_reason or confidence:
        data["confidence"] = confidence | {
            "level": row.confidence_level,
            "reason": row.confidence_reason,
        }
    if row.feature_version is not None:
        data["feature_version"] = row.feature_version
    return Fact.model_validate(data | extra)


def hotel_dict(h: Hotel) -> dict[str, Any]:
    return {
        "hotel_id": h.id,
        "city_id": h.city_id,
        "name": h.name,
        "address": h.address,
        "latitude": h.latitude,
        "longitude": h.longitude,
        "hotel_kind": h.kind,
        "hotel_url": (h.source or {}).get("url"),
        "subzone": h.subzone,
        "stars": h.stars,
    }


class DbStore:
    stub = False

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _session(self) -> Session:
        return Session(self.engine)

    def cities(self) -> list[dict[str, Any]]:
        with self._session() as s:
            counts = dict(
                s.execute(select(Hotel.city_id, func.count()).group_by(Hotel.city_id)).all()
            )
            return [
                {
                    "city_id": c.id,
                    "name": c.name,
                    "depth": c.depth,
                    "timezone": c.timezone,
                    "hotels": counts.get(c.id, 0),
                    "hotels_in_sample": counts.get(c.id, 0),
                }
                # Полная глубина (Сочи, Москва) — первыми, дальше по названию (D36).
                for c in s.scalars(select(City).order_by(City.depth.desc(), City.name))
            ]

    def coverage(self, city_id: str) -> list[dict[str, Any]] | None:
        with self._session() as s:
            if s.get(City, city_id) is None:
                return None
            rows = s.scalars(
                select(CityCoverage)
                .where(CityCoverage.city_id == city_id)
                .order_by(CityCoverage.feature_id)
            )
            return [
                {
                    "feature_id": r.feature_id,
                    "feature_name": r.feature_name,
                    "N": r.n_hotels,
                    "computed": r.computed,
                    "estimate": r.estimate,
                    "unknown": r.unknown,
                    "not_applicable": r.not_applicable,
                }
                for r in rows
            ]

    def hotels(self, city_id: str | None = None) -> list[dict[str, Any]]:
        q = select(Hotel).order_by(Hotel.city_id, Hotel.name)
        if city_id:
            q = q.where(Hotel.city_id == city_id)
        with self._session() as s:
            return [hotel_dict(h) for h in s.scalars(q)]

    def hotel(self, hotel_id: str) -> dict[str, Any] | None:
        with self._session() as s:
            h = s.get(Hotel, hotel_id)
            return hotel_dict(h) if h else None

    def hotel_facts(self, hotel_id: str) -> dict[str, list[Fact]]:
        q = (
            select(FactRow)
            .where(FactRow.hotel_id == hotel_id, FactRow.is_current)
            .order_by(FactRow.feature_id, FactRow.fact_id)
        )
        out: dict[str, list[Fact]] = defaultdict(list)
        with self._session() as s:
            for row in s.scalars(q):
                out[row.feature_id].append(fact_from_row(row))
        return dict(out)

    def fact(self, fact_id: str) -> Fact | None:
        with self._session() as s:
            row = s.get(FactRow, fact_id)
            return fact_from_row(row) if row else None

    def facts_for(self, feature_id: str, hotel_ids: Collection[str]) -> dict[str, list[Fact]]:
        if not hotel_ids:
            return {}
        q = select(FactRow).where(
            FactRow.feature_id == feature_id,
            FactRow.hotel_id.in_(list(hotel_ids)),
            FactRow.is_current,
        )
        out: dict[str, list[Fact]] = defaultdict(list)
        with self._session() as s:
            for row in s.scalars(q):
                out[row.hotel_id].append(fact_from_row(row))
        return dict(out)

    def room_types(self, hotel_ids: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        if not hotel_ids:
            return {}
        ids = list(hotel_ids)
        with self._session() as s:
            rows = s.execute(
                select(RoomType, PlacementSet)
                .join(PlacementSet, PlacementSet.room_type_id == RoomType.id, isouter=True)
                .where(RoomType.hotel_id.in_(ids))
                .order_by(RoomType.hotel_id, RoomType.name)
            ).all()
            rt_ids = [rt.id for rt, _ in rows]
            placements: dict[str, list[dict[str, Any]]] = defaultdict(list)
            by_id: dict[str, dict[str, Any]] = {}
            if rt_ids:
                for p in s.scalars(
                    select(Placement)
                    .where(Placement.room_type_id.in_(rt_ids))
                    .order_by(Placement.building_id, Placement.floor)
                ):
                    item = {
                        "placement_id": p.id,
                        "building_id": p.building_id,
                        "floor": p.floor,
                        "floor_label": p.floor_label,
                        "windows": [],
                    }
                    placements[p.room_type_id].append(item)
                    by_id[p.id] = item
                if by_id:
                    for w in s.scalars(
                        select(PlacementWindow)
                        .where(PlacementWindow.placement_id.in_(list(by_id)))
                        .order_by(PlacementWindow.facade_id)
                    ):
                        by_id[w.placement_id]["windows"].append(w.facade_id)
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rt, ps in rows:
            out[rt.hotel_id].append(
                {
                    "room_type_id": rt.id,
                    "name": rt.name,
                    "placement_set": (
                        {
                            "completeness": ps.completeness,
                            "guarantee": ps.guarantee,
                            "status": ps.status,
                            "source": ps.source,
                        }
                        if ps
                        else None
                    ),
                    "placements": placements.get(rt.id, []),
                }
            )
        return dict(out)

    def questions(self, hotel_id: str) -> list[dict[str, Any]]:
        q = (
            select(Question, Hotel.city_id)
            .join(Hotel, Hotel.id == Question.hotel_id)
            .where(Question.hotel_id == hotel_id)
            .order_by(Question.feature_id)
        )
        with self._session() as s:
            return [
                {
                    "question_id": qu.id,
                    "hotel_id": qu.hotel_id,
                    "city_id": city_id,
                    "feature_id": qu.feature_id,
                    "question": qu.text,
                    "parsed_fields": qu.fields or [],
                    "answer_text": qu.answer_text,
                    "is_simulated": qu.is_simulated,
                    "answer_status": qu.state,
                    "claims": qu.prefill or [],
                }
                for qu, city_id in s.execute(q)
            ]

    def conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        q = (
            select(OtaComparison, Hotel.name)
            .join(Hotel, Hotel.id == OtaComparison.hotel_id, isouter=True)
            .order_by(OtaComparison.city_id, OtaComparison.hotel_id, OtaComparison.feature_id)
        )
        if city_id:
            q = q.where(OtaComparison.city_id == city_id)
        with self._session() as s:
            return [
                {
                    "city_id": c.city_id,
                    "hotel_id": c.hotel_id,
                    "name": name,
                    "feature_id": c.feature_id,
                    "ota_value": c.ota_value,
                    "other_value": c.other_value,
                    "classification": c.classification,
                    "numeric_comparison": c.numeric_comparison,
                    "resolution": c.resolution,
                    "ota_url": (c.source or {}).get("url"),
                    "ota_pointer": (c.source or {}).get("json_pointer"),
                }
                for c, name in s.execute(q)
            ]
