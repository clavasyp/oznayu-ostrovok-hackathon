"""Таблицы PostgreSQL + PostGIS (core.md §2). Контракт 1, часть «база».

ID — текстовые, как в v9: `masterId` Островка для отелей, `way/…` для корпусов OSM,
24-символьный hex для фактов. Так факты v9 грузятся без адаптера.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class City(Base):
    __tablename__ = "city"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "moscow", "sochi"
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(Text, default="automatic")  # full | automatic (D36)


class Hotel(Base):
    __tablename__ = "hotel"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # masterId Островка
    city_id: Mapped[str] = mapped_column(ForeignKey("city.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    stars: Mapped[float | None] = mapped_column(Float)
    subzone: Mapped[str | None] = mapped_column(Text)  # для Сочи: центр, Адлер, Сириус…
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    geom: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326))
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # url, sha256, json_pointer


class RoomType(Base):
    """Категория номера Островка; ключ — (masterId, rgHash)."""

    __tablename__ = "room_type"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # f"{hotel_id}:{rg_hash}"
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotel.id"), index=True)
    rg_hash: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    amenities: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Building(Base):
    __tablename__ = "building"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "way/260633951"
    city_id: Mapped[str] = mapped_column(ForeignKey("city.id"), index=True)
    geom: Mapped[Any] = mapped_column(Geometry("GEOMETRY", srid=4326))
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class HotelBuilding(Base):
    """Связь отель ↔ корпус. Происхождение обязательно: автоматика или проверено командой (D33)."""

    __tablename__ = "hotel_building"

    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotel.id"), primary_key=True)
    building_id: Mapped[str] = mapped_column(ForeignKey("building.id"), primary_key=True)
    method: Mapped[str] = mapped_column(Text)  # auto | curated
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Feature(Base):
    """Спецификация характеристики (YAML из specs/), версионируется."""

    __tablename__ = "feature"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "W01"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    family: Mapped[str] = mapped_column(Text)
    subject_level: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")  # active | draft | retired
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)


class FactRow(Base):
    """Факт с паспортом. Поля — как `core.facts.Fact`; диагностика v9 — в `extra`."""

    __tablename__ = "fact"

    fact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hotel_id: Mapped[str] = mapped_column(Text, index=True)
    city_id: Mapped[str] = mapped_column(Text)
    subject_level: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    feature_id: Mapped[str] = mapped_column(Text)
    feature_version: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    value: Mapped[Any] = mapped_column(JSONB, nullable=True)  # у unknown значения нет
    unit: Mapped[str | None] = mapped_column(Text)
    quantifier: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    unknown_reason: Mapped[str | None] = mapped_column(Text)
    confidence_level: Mapped[str | None] = mapped_column(Text)
    confidence_reason: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    recipe_id: Mapped[str | None] = mapped_column(Text)
    algorithm_version: Mapped[str | None] = mapped_column(Text)
    recipe_attempts: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    depends_on: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    hard_condition_eligible: Mapped[bool | None] = mapped_column(Boolean)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_after: Mapped[Any] = mapped_column(JSONB, nullable=True)
    scope_note: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_fact_hotel_feature", "hotel_id", "feature_id"),
        Index("ix_fact_city_feature_status", "city_id", "feature_id", "status"),
    )


class Conflict(Base):
    __tablename__ = "conflict"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feature_id: Mapped[str] = mapped_column(Text)
    hotel_id: Mapped[str] = mapped_column(Text, index=True)
    subject_level: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    fact_a: Mapped[str] = mapped_column(Text)
    fact_b: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        Text, default="detected"
    )  # detected | awaiting_evidence | resolved | reopened
    resolution_rule: Mapped[str | None] = mapped_column(Text)
    resolved_fact: Mapped[str | None] = mapped_column(Text)
    question_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Question(Base):
    """Вопрос отельеру и его ответ (core.md §7.2)."""

    __tablename__ = "question"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotel_id: Mapped[str] = mapped_column(Text, index=True)
    feature_id: Mapped[str] = mapped_column(Text)
    subject_level: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    prefill: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    fields: Mapped[list[str]] = mapped_column(JSONB, default=list)  # какие поля ждём в ответе
    priority: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text)  # unknown | conflict | low_confidence | expired
    state: Mapped[str] = mapped_column(Text, default="open")
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)


class CurationItem(Base):
    """Кандидат ручной обработки с ИИ (D33). Фактом `curated` становится только после проверки."""

    __tablename__ = "curation_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # match_hotel_building | beach_entrance | view_check …
    subject_level: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    feature_id: Mapped[str | None] = mapped_column(Text)
    proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposed_by: Mapped[str] = mapped_column(Text)  # "llm:<model>" | "script:<name>" | "codex"
    evidence: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    state: Mapped[str] = mapped_column(
        Text, default="proposed"
    )  # proposed | accepted | corrected | rejected | unsure
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_minutes: Mapped[float | None] = mapped_column(Float)
    estimated_review_minutes: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)
    fact_id: Mapped[str | None] = mapped_column(Text)


class CurationClaim(Base):
    """Дочернее утверждение кандидата (v11): кухня, часы, часы кухни, координаты, рейтинг —
    у каждого своё доказательство. Человек принимает конкретные поля, не весь набор сразу."""

    __tablename__ = "curation_claim"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    parent_item_id: Mapped[int] = mapped_column(ForeignKey("curation_item.id"), index=True)
    feature_id: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    original_proposal: Mapped[dict[str, Any]] = mapped_column(JSONB)
    corrected_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    state: Mapped[str] = mapped_column(
        Text, default="proposed"
    )  # proposed | accepted | corrected | rejected | unsure
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_minutes: Mapped[float | None] = mapped_column(Float)
    fact_id: Mapped[str | None] = mapped_column(Text)


class UnmatchedAtom(Base):
    """Событие «часть запроса вне каталога» (D34, v11). Частота — число разных запросов в окне,
    а не счётчик в строке: синтетика и отзывы не смешиваются с живым потоком (source_stratum)."""

    __tablename__ = "unmatched_atom"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True)
    query_id: Mapped[str] = mapped_column(Text, index=True)  # псевдоним запроса или сессии
    atom_id: Mapped[str] = mapped_column(Text)
    source_stratum: Mapped[str] = mapped_column(Text, default="live")  # live | review | synthetic
    city_id: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, default="ru")
    span_text: Mapped[str] = mapped_column(Text)  # отредактированный фрагмент
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[str] = mapped_column(Text, index=True)
    requiredness: Mapped[str] = mapped_column(Text, default="unspecified")
    mapping_status: Mapped[str] = mapped_column(Text, default="unmatched")
    nearest_contracts: Mapped[list[str]] = mapped_column(JSONB, default=list)
    state: Mapped[str] = mapped_column(
        Text, default="new"
    )  # new | triaged | data_task | draft_spec | activated | closed
    linked_feature_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OtaComparison(Base):
    """Сверка наших фактов с карточкой Островка (v9): совпало, расходится или сравнить нечем."""

    __tablename__ = "ota_comparison"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    city_id: Mapped[str] = mapped_column(Text, index=True)
    hotel_id: Mapped[str] = mapped_column(Text, index=True)
    feature_id: Mapped[str] = mapped_column(Text)
    ota_value: Mapped[str | None] = mapped_column(Text)
    other_value: Mapped[Any] = mapped_column(JSONB, nullable=True)
    classification: Mapped[str] = mapped_column(Text)
    numeric_comparison: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # ota_url, ota_pointer
    pack_version: Mapped[str | None] = mapped_column(Text)


class CityCoverage(Base):
    """Покрытие характеристики в городе: сколько отелей посчитано, оценено, неизвестно (core §6)."""

    __tablename__ = "city_coverage"

    city_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_id: Mapped[str] = mapped_column(Text, primary_key=True)
    feature_name: Mapped[str | None] = mapped_column(Text)
    n_hotels: Mapped[int] = mapped_column(Integer)
    computed: Mapped[int] = mapped_column(Integer, default=0)
    estimate: Mapped[int] = mapped_column(Integer, default=0)
    unknown: Mapped[int] = mapped_column(Integer, default=0)
    not_applicable: Mapped[int] = mapped_column(Integer, default=0)


class PackLoad(Base):
    """Какие пакеты городов загружены: init не грузит повторно, /health знает готовность."""

    __tablename__ = "pack_load"

    city_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pack_version: Mapped[str] = mapped_column(Text)
    manifest_sha256: Mapped[str] = mapped_column(Text)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
