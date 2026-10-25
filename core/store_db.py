"""Хранилище на PostgreSQL + PostGIS (`DATA_MODE=db`): те же ответы, что у заглушек, но из базы,
куда `init` загрузил пакеты городов.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.db import (
    City,
    CityCoverage,
    Conflict,
    FactRow,
    Hotel,
    OtaComparison,
    Placement,
    PlacementSet,
    PlacementWindow,
    Question,
    RoomType,
    UnmatchedAtom,
)
from core.facts import Fact
from core.hotelier import AnswerError, AnswerResult

FACT_COLUMNS = [c.name for c in FactRow.__table__.columns]
FACT_COLUMN_SET = set(FACT_COLUMNS)
# Служебные колонки таблицы, которых нет в контракте факта.
NOT_IN_FACT = {
    "confidence_level",
    "confidence_reason",
    "extra",
    "is_current",
    "superseded_by",
    "feature_version",
}


def fact_to_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Факт → строка таблицы `fact`. Валидация по контракту; лишние поля v9 → extra."""
    fact = Fact.model_validate(raw)
    data = fact.model_dump(mode="json")
    confidence = data.pop("confidence", None) or {}
    row = {k: v for k, v in data.items() if k in FACT_COLUMN_SET}
    row["confidence_level"] = confidence.pop("level", None)
    row["confidence_reason"] = confidence.pop("reason", None)
    row["extra"] = {k: v for k, v in data.items() if k not in FACT_COLUMN_SET}
    if confidence:  # прочие поля уверенности, если калькулятор их дал
        row["extra"]["confidence"] = confidence
    return row


def fact_from_row(row: FactRow) -> Fact:
    """Строка таблицы `fact` → контракт `Fact`: обратное к `fact_to_row`."""
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

    @staticmethod
    def _question_dict(qu: Question, city_id: str | None) -> dict[str, Any]:
        return {
            "question_id": qu.id,
            "hotel_id": qu.hotel_id,
            "city_id": city_id,
            "feature_id": qu.feature_id,
            "question": qu.text,
            "parsed_fields": qu.fields or [],
            "answer_text": qu.answer_text,
            "is_simulated": qu.is_simulated,
            "answer_status": qu.state,
            "asked_at": qu.asked_at.isoformat() if qu.asked_at else None,
            "answered_at": qu.answered_at.isoformat() if qu.answered_at else None,
            "parsed": qu.parsed,
            "claims": qu.prefill or [],
        }

    def questions(self, hotel_id: str) -> list[dict[str, Any]]:
        q = (
            select(Question, Hotel.city_id)
            .join(Hotel, Hotel.id == Question.hotel_id)
            .where(Question.hotel_id == hotel_id)
            .order_by(Question.feature_id)
        )
        with self._session() as s:
            return [self._question_dict(qu, city_id) for qu, city_id in s.execute(q)]

    def question(
        self, hotel_id: str, question_id: int | None, feature_id: str | None
    ) -> dict[str, Any] | None:
        q = (
            select(Question, Hotel.city_id)
            .join(Hotel, Hotel.id == Question.hotel_id)
            .where(Question.hotel_id == hotel_id)
        )
        if question_id is not None:
            q = q.where(Question.id == question_id)
        else:
            q = q.where(Question.feature_id == feature_id)
        with self._session() as s:
            row = s.execute(q.limit(1)).first()
            return self._question_dict(*row) if row else None

    def set_question_state(self, question_id: int, state: str, at: datetime) -> None:
        with self._session() as s, s.begin():
            s.execute(
                update(Question).where(Question.id == question_id).values(state=state, asked_at=at)
            )

    def apply_answer(self, result: AnswerResult) -> None:
        with self._session() as s, s.begin():
            s.execute(
                update(Question)
                .where(Question.id == result.question_id)
                .values(
                    state=result.state,
                    answer_text=result.answer_text,
                    answered_at=result.answered_at,
                    parsed=result.parsed,
                    is_simulated=result.is_simulated,
                )
            )
            for f in result.facts:
                stmt = insert(FactRow).values(fact_to_row(f.model_dump(mode="json")))
                s.execute(stmt.on_conflict_do_nothing(index_elements=["fact_id"]))
            for old, new in result.superseded.items():
                s.execute(
                    update(FactRow)
                    .where(FactRow.fact_id == old)
                    .values(is_current=False, superseded_by=new)
                )
            for ours, theirs in result.conflicts:
                s.add(
                    Conflict(
                        feature_id=theirs.feature_id,
                        hotel_id=theirs.hotel_id,
                        subject_level=theirs.subject_level,
                        subject_id=theirs.subject_id,
                        fact_a=ours.fact_id,
                        fact_b=theirs.fact_id,
                        state="detected",
                    )
                )
            if result.placements:
                self._replace_placements(s, result)

    @staticmethod
    def _replace_placements(s: Session, result: AnswerResult) -> None:
        rt_id = result.room_type_id
        if rt_id is None or s.get(RoomType, rt_id) is None:
            raise AnswerError(f"нет категории {rt_id}: размещения не к чему привязать")
        old = select(Placement.id).where(Placement.room_type_id == rt_id).scalar_subquery()
        s.execute(delete(PlacementWindow).where(PlacementWindow.placement_id.in_(old)))
        s.execute(delete(Placement).where(Placement.room_type_id == rt_id))
        s.execute(delete(PlacementSet).where(PlacementSet.room_type_id == rt_id))
        ps = result.placements
        s.add(
            PlacementSet(
                room_type_id=rt_id,
                completeness=ps.completeness,
                guarantee=ps.guarantee,
                status=result.placement_status,
                source=result.placement_source,
            )
        )
        s.flush()
        windows: dict[str, set[str]] = defaultdict(set)
        for item in ps.items:
            pid = f"{rt_id}|{item.building_id}|{item.floor}"
            if pid not in windows:
                s.add(
                    Placement(
                        id=pid,
                        room_type_id=rt_id,
                        building_id=item.building_id,
                        building_part_id=item.building_part_id,
                        floor=item.floor,
                        floor_label=item.floor_label,
                    )
                )
            windows[pid] |= set(item.windows)
        s.flush()
        for pid, facades in windows.items():
            for facade in sorted(facades):
                s.add(PlacementWindow(placement_id=pid, facade_id=facade))

    def fact_conflicts(self, city_id: str | None = None) -> list[dict[str, Any]]:
        q = (
            select(Conflict, Hotel.city_id)
            .join(Hotel, Hotel.id == Conflict.hotel_id, isouter=True)
            .order_by(Conflict.updated_at.desc())
        )
        if city_id:
            q = q.where(Hotel.city_id == city_id)
        with self._session() as s:
            return [
                {
                    "conflict_id": c.id,
                    "city_id": city,
                    "hotel_id": c.hotel_id,
                    "feature_id": c.feature_id,
                    "subject_id": c.subject_id,
                    "fact_a": c.fact_a,
                    "fact_b": c.fact_b,
                    "state": c.state,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                }
                for c, city in s.execute(q)
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

    def record_unmatched(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with self._session() as s, s.begin():
            s.execute(
                insert(UnmatchedAtom)
                .values(events)
                .on_conflict_do_nothing(index_elements=["event_id"])
            )

    def unmatched_events(
        self, city_id: str | None = None, stratum: str | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        q = select(UnmatchedAtom).order_by(UnmatchedAtom.created_at)
        if city_id:
            q = q.where(UnmatchedAtom.city_id == city_id)
        if stratum:
            q = q.where(UnmatchedAtom.source_stratum == stratum)
        if since:
            q = q.where(UnmatchedAtom.created_at >= since)
        columns = [c.name for c in UnmatchedAtom.__table__.columns if c.name != "id"]
        with self._session() as s:
            return [{c: getattr(row, c) for c in columns} for row in s.scalars(q)]
