"""Хранилище на PostgreSQL + PostGIS (`DATA_MODE=db`): те же ответы, что у заглушек, но из базы,
куда `init` загрузил пакеты городов.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core import freshness, live
from core.db import (
    Building,
    City,
    CityCoverage,
    Conflict,
    CurationItem,
    FactRow,
    FeatureSpecRow,
    Hotel,
    HotelBuilding,
    OtaComparison,
    Placement,
    PlacementSet,
    PlacementWindow,
    Question,
    RoomType,
    UnmatchedAtom,
)
from core.facts import Fact
from core.geo_store import GeoStore
from core.hotelier import AnswerError, AnswerResult, placement_id, replaces_placements
from core.placement_catalog import with_alias

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


# Поиску хватает значения, статуса и места факта; паспорт (источники, попытки рецептов, оговорки
# — это бо́льшая часть строки) объяснение берёт по id (`fact`). `depends_on` — сторона окна.
SEARCH_COLUMNS = (
    "fact_id",
    "hotel_id",
    "city_id",
    "subject_level",
    "subject_id",
    "feature_id",
    "params",
    "value",
    "unit",
    "quantifier",
    "status",
    "unknown_reason",
    "hard_condition_eligible",
    "algorithm_version",
    "depends_on",
)


def search_fact(row: Any) -> Fact:
    """Строка `SEARCH_COLUMNS` → `Fact` без паспорта (только для поиска)."""
    return Fact.model_validate({k: v for k, v in row._mapping.items() if v is not None})


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
        "subzone": h.subzone,  # для гостя: «Адлер», «Красная Поляна»
        "subzone_id": h.subzone_id,  # код для фильтров: adler, krasnaya_polyana
        "stars": h.stars,
    }


FRESHNESS_TTL_S = 600


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

    def freshness(self, city_id: str, as_of: datetime) -> list[dict[str, Any]]:
        """Свежесть одним запросом: срок — свой `refresh_after` (дата строкой в JSONB) или дата
        значения + срок по виду данных (`core.freshness.MAX_AGE_DAYS`). Запрос идёт по всем
        фактам города (в Сочи — 630 тыс.), поэтому ответ на дату кешируется на 10 минут."""
        cache = self.__dict__.setdefault("_freshness", {})
        key = (city_id, as_of.date().isoformat())
        hit = cache.get(key)
        if hit and time.monotonic() - hit[0] < FRESHNESS_TTL_S:
            return hit[1]
        days = " ".join(f"WHEN '{k}' THEN {v}" for k, v in freshness.MAX_AGE_DAYS.items())
        sql = text(f"""
            WITH f AS (
              SELECT feature_id, status, coalesce(algorithm_version, recipe_id, '') AS recipe,
                     coalesce(calculated_at, observed_at) AS dated,
                     coalesce(
                       CASE WHEN refresh_after #>> '{{}}' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                            THEN (refresh_after #>> '{{}}')::timestamptz END,
                       valid_to,
                       coalesce(calculated_at, observed_at)
                         + make_interval(days => CASE status {days} END)
                     ) AS due
              FROM fact
              WHERE city_id = :city AND is_current AND status <> 'unknown'
            )
            SELECT feature_id, status, recipe, count(*) AS n, min(dated) AS oldest,
                   min(due) AS next_due, count(*) FILTER (WHERE due <= :as_of) AS stale
            FROM f GROUP BY 1, 2, 3
            ORDER BY stale DESC, next_due NULLS LAST, feature_id
        """)  # noqa: S608 — подставляются только константы MAX_AGE_DAYS
        with self._session() as s:
            rows = s.execute(sql, {"city": city_id, "as_of": as_of}).all()
        out = [freshness.row(*r) for r in rows]
        cache[key] = (time.monotonic(), out)
        return out

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
            return fact_from_row(row) if row else live.recent(fact_id)

    def facts_for(
        self,
        feature_id: str,
        hotel_ids: Collection[str],
        params: dict[str, Any] | None = None,
        key_params: Collection[str] = (),
    ) -> dict[str, list[Fact]]:
        if not hotel_ids:
            return {}
        base = (
            FactRow.feature_id == feature_id,
            FactRow.hotel_id.in_(list(hotel_ids)),
            FactRow.is_current,
        )
        cols = [FactRow.__table__.c[c] for c in SEARCH_COLUMNS]
        q = select(*cols).where(*base)
        if params is not None:
            # Факты окон — по датам (V05): без фильтра в SQL поиск разбирал бы все даты сразу.
            # Ключевой параметр — сравнением `params ->> key` (по нему индекс
            # `ix_fact_feature_hotel_date`), остальные — вхождением.
            rest = {k: v for k, v in params.items() if k not in key_params}
            if rest:
                q = q.where(FactRow.params.contains(rest))
            for k in key_params:
                value = FactRow.params[k].astext
                q = q.where(value == str(params[k]) if k in params else value.is_(None))
        out: dict[str, list[Fact]] = defaultdict(list)
        with self._session() as s:
            for row in s.execute(q):
                out[row.hotel_id].append(search_fact(row))
            rest = [h for h in hotel_ids if h not in out]
            if params is not None and rest:
                # Факты есть, но с другими параметрами — по одному на отель (см. `Store`).
                q = (
                    select(*cols)
                    .where(*base[:1], FactRow.hotel_id.in_(rest), FactRow.is_current)
                    .distinct(FactRow.hotel_id)
                    .order_by(FactRow.hotel_id, FactRow.fact_id)
                )
                for row in s.execute(q):
                    out[row.hotel_id].append(search_fact(row))
        return dict(out)

    @staticmethod
    def _building_names(s: Session, building_ids: set[str]) -> dict[str, str]:
        """Как назвать корпус гостю: название со слов отеля, иначе имя или адрес по карте —
        не «way/123» (идентификатор OSM — для паспорта, а не для подписи)."""
        out: dict[str, str] = {}
        if not building_ids:
            return out
        for link in s.scalars(
            select(HotelBuilding).where(HotelBuilding.building_id.in_(list(building_ids)))
        ):
            aliases = (link.evidence or {}).get("hotel_aliases") or []
            if aliases and aliases[0].get("name"):
                out.setdefault(link.building_id, aliases[0]["name"])
        for b in s.scalars(select(Building).where(Building.id.in_(list(building_ids)))):
            tags = b.tags or {}
            street = " ".join(
                x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x
            )
            label = tags.get("name") or street
            if label:
                out.setdefault(b.id, label)
        return out

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
                    names = self._building_names(s, {(p["building_id"]) for p in by_id.values()})
                    for item in by_id.values():
                        item["building_name"] = names.get(item["building_id"])
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

    def placement_geometry(self, hotel_id: str) -> dict[str, Any]:
        return GeoStore(self.engine).geometry(hotel_id)

    def nearest_places(
        self,
        hotel_ids: Collection[str],
        category: str,
        radius_m: float,
        rank: Callable[[dict[str, Any]], int | None] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        return GeoStore(self.engine).nearest(list(hotel_ids), category, radius_m, rank)

    def place_counts(
        self, hotel_ids: Collection[str], category: str, radius_m: float
    ) -> tuple[dict[str, dict[str, Any]], int]:
        return GeoStore(self.engine).place_counts(list(hotel_ids), category, radius_m)

    def find_target(self, city_id: str, text: str) -> dict[str, Any] | None:
        return GeoStore(self.engine).find_target(city_id, text)

    def distances(self, hotel_ids: Collection[str], target: dict[str, Any]) -> dict[str, int]:
        return GeoStore(self.engine).distances(list(hotel_ids), target)

    def save_feature(
        self,
        feature: dict[str, Any],
        spec_yaml: str,
        facts: list[Fact],
        cities: dict[str, Any],
        source_text: str | None,
    ) -> None:
        """Включить характеристику: спецификация в `feature_spec`, факты расчёта — вместо прежних
        фактов этой характеристики (повторное включение пересчитывает), в одной транзакции."""
        row = {
            "id": feature["id"],
            "version": feature.get("spec_version", 1),
            "status": "active",
            "title": feature["title"],
            "yaml": spec_yaml,
            "feature": feature,
            "cities": cities,
            "source_text": source_text,
        }
        rows = [fact_to_row(f.model_dump(mode="json")) for f in facts]
        with Session(self.engine) as s, s.begin():
            s.execute(
                insert(FeatureSpecRow)
                .values(row)
                .on_conflict_do_update(
                    index_elements=[FeatureSpecRow.id],
                    set_={k: v for k, v in row.items() if k != "id"} | {"activated_at": func.now()},
                )
            )
            s.execute(delete(FactRow).where(FactRow.feature_id == feature["id"]))
            for i in range(0, len(rows), 2000):
                s.execute(insert(FactRow), rows[i : i + 2000])

    def feature_specs(self) -> list[dict[str, Any]]:
        """Включённые характеристики (записи каталога) — каталог подхватывает их при старте."""
        with self._session() as s:
            return [
                {**r.feature, "cities": r.cities, "activated_at": r.activated_at.isoformat()}
                for r in s.scalars(
                    select(FeatureSpecRow)
                    .where(FeatureSpecRow.status == "active")
                    .order_by(FeatureSpecRow.id)
                )
            ]

    def add_building_alias(self, hotel_id: str, building_id: str, alias: dict[str, Any]) -> bool:
        with self._session() as s, s.begin():
            link = s.get(HotelBuilding, (hotel_id, building_id))
            if link is None:
                return False
            link.evidence = with_alias(link.evidence, alias)
        return True

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
            if isinstance(result.question_id, int):  # у вопроса о размещении (`qp_…`) строки нет
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
        prev = s.get(PlacementSet, rt_id)
        if prev is not None and not replaces_placements(result.placement_status, prev.status):
            # Ответ имитатора не заменяет набор настоящего отеля или проверенный командой (D41-8).
            result.placements_kept, result.placements = prev.status, None
            return
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
            pid = placement_id(rt_id, item)
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

    def curation_queue(
        self, city_id: str | None, state: str, limit: int, offset: int
    ) -> dict[str, Any]:
        """Очередь D33: сначала кандидаты, которые быстрее проверить; у отеля — название, адрес и
        точка, у кандидата — здание, адрес по карте и совпадение с адресом отеля."""
        where = [CurationItem.city_id == city_id] if city_id else []
        with self._session() as s:
            counts = dict(
                s.execute(
                    select(CurationItem.state, func.count())
                    .where(*where)
                    .group_by(CurationItem.state)
                ).all()
            )
            q = (
                select(CurationItem, Hotel.name, Hotel.address, Hotel.latitude, Hotel.longitude)
                .join(Hotel, Hotel.id == CurationItem.subject_id, isouter=True)
                .where(*where, CurationItem.state == state)
                .order_by(
                    # Сначала те, где есть из чего выбрать: кандидаты-здания с доказательствами.
                    func.coalesce(
                        func.jsonb_array_length(CurationItem.proposal["candidate_building_ids"]), 0
                    )
                    == 0,
                    CurationItem.estimated_review_minutes,
                    CurationItem.id,
                )
                .limit(limit)
                .offset(offset)
            )
            items = [
                _curation_row(c, name, address, lat, lon)
                for c, name, address, lat, lon in s.execute(q)
            ]
        return {"total": counts.get(state, 0), "counts": counts, "items": items}

    def review_curation(
        self, item_id: int, state: str, reviewer: str, note: str | None, building_id: str | None
    ) -> dict[str, Any] | None:
        with self._session() as s, s.begin():
            item = s.get(CurationItem, item_id)
            if item is None:
                return None
            candidates = (item.proposal or {}).get("candidate_building_ids") or []
            if building_id and building_id not in candidates:
                raise ValueError("здание не из списка кандидатов")
            if state == "accepted" and item.kind == "match_hotel_building" and not building_id:
                raise ValueError("для «это здание» нужен building_id кандидата")
            item.state = state
            item.reviewer = reviewer
            item.reviewed_at = datetime.now(UTC)
            item.note = note or item.note
            if building_id:
                item.proposal = {**(item.proposal or {}), "building_id": building_id}
            s.flush()
            return _curation_row(item, None, None, None, None)

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


def _curation_row(
    c: CurationItem, name: str | None, address: str | None, lat: float | None, lon: float | None
) -> dict[str, Any]:
    """Элемент очереди D33 для экрана: кандидаты-здания с доказательствами одним списком."""
    proposal = c.proposal or {}
    found: dict[str, Any] = {}
    for e in c.evidence or []:
        for value in e.get("values") or []:
            found.update(value.get("evidence") or {})
    candidates = [
        {
            "building_id": bid,
            "osm_address": (found.get(bid) or {}).get("normalized_osm"),
            "house": ((found.get(bid) or {}).get("comparison") or {}).get("house"),
            "street": ((found.get(bid) or {}).get("comparison") or {}).get("street"),
            "point_in_building": (found.get(bid) or {}).get("point_covered_by_building"),
            "url": (found.get(bid) or {}).get("source_url")
            or f"https://www.openstreetmap.org/{bid}",
        }
        for bid in proposal.get("candidate_building_ids") or []
    ]
    hotel_address = next(
        (e.get("value") for e in c.evidence or [] if e.get("kind") == "hotel_address"), None
    )
    # Возможный дубль объявления: две карточки Островка в одной точке с тем же названием.
    listings = [
        {k: e.get(k) for k in ("hotel_id", "name", "address", "url", "room_types")}
        for e in c.evidence or []
        if e.get("kind") == "ostrovok_listing"
    ]
    return {
        "id": str(c.id),  # большие id — строкой: в JS число теряет точность
        "city_id": c.city_id,
        "kind": c.kind,
        "subject_id": c.subject_id,
        "hotel_name": name,
        "hotel_address": address or hotel_address,
        "latitude": lat,
        "longitude": lon,
        "reason": proposal.get("reason"),
        "candidates": candidates,
        "listings": listings,
        "chosen_building_id": proposal.get("building_id"),
        "state": c.state,
        "reviewer": c.reviewer,
        "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
        "note": c.note,
        "estimated_review_minutes": c.estimated_review_minutes,
        "proposed_by": c.proposed_by,
    }
