"""Кандидаты «отель → здание» по адресу (задача 3б, D33): строки `curation_item` для проверки.

Связь не устанавливаем сами: только предлагаем с доказательством (адрес отеля, адрес здания OSM,
расстояние от точки Островка, связь v9, если была). Решение о связи и геометрию делает Сева,
принимает человек. Ближайшее здание без совпадения адреса кандидатом не считается — как и в v9
(«ближайший контур без полного адреса не принимается»); оно идёт только подсказкой.
"""

from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from agents.address.normalize import Address, osm_addresses, parse_address

NEAR_M = 300  # совпал адрес и здание рядом с точкой Островка — уверенный кандидат
FAR_M = 1500  # дальше — адрес совпал, но точка отеля, похоже, не там: проверить вручную
SIMILAR_STREET = 0.88  # «Московкая» и «Московская» — опечатка, а не другая улица
REVIEW_MINUTES = {"high": 0.5, "medium": 1.5, "ambiguous": 3.0, "low": 3.0, "none": 5.0}


@dataclass(frozen=True)
class Building:
    id: str
    tags: dict[str, str]
    rings: tuple[tuple[tuple[float, float], ...], ...]  # внешние кольца, (lon, lat)
    addresses: tuple[Address, ...]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Building:
        geom = row.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "Polygon":
            rings = [coords[0]] if coords else []
        elif geom.get("type") == "MultiPolygon":
            rings = [poly[0] for poly in coords if poly]
        elif geom.get("type") == "Point":
            rings = [[coords]]
        else:
            rings = []
        tags = {k: str(v) for k, v in (row.get("tags") or {}).items()}
        return cls(
            id=row["id"],
            tags=tags,
            rings=tuple(tuple((float(x), float(y)) for x, y, *_ in ring) for ring in rings),
            addresses=tuple(osm_addresses(tags)),
        )


def load_buildings(path: Path) -> list[Building]:
    """Корпуса из пакета города (`buildings.jsonl.gz`, packs/FORMAT.md)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [Building.from_row(json.loads(line)) for line in f if line.strip()]


def _meters(lat: float, lon: float, x: float, y: float) -> tuple[float, float]:
    k = 111_320.0
    return (x - lon) * k * math.cos(math.radians(lat)), (y - lat) * k


def distance_m(lat: float, lon: float, b: Building) -> float:
    """От точки отеля до контура: 0 — точка внутри, иначе до ближайшей вершины или ребра."""
    best = math.inf
    for ring in b.rings:
        pts = [_meters(lat, lon, x, y) for x, y in ring]
        if len(pts) >= 3 and _inside(pts):
            return 0.0
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
            best = min(best, _to_segment(x1, y1, x2, y2))
    return best


def _inside(pts: list[tuple[float, float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        if (y1 > 0) != (y2 > 0) and 0 < (x2 - x1) * (0 - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _to_segment(x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / (dx * dx + dy * dy)))
    return math.hypot(x1 + t * dx, y1 + t * dy)


def _base(house: str) -> str:
    return house.rstrip("абвгдежзиклмнопрстуфхцчшщэюя")


def compare(hotel: Address, building: Address) -> dict[str, str] | None:
    """Совпадение адресов: улица (`same` / `similar`) и дом (`exact` / `partial`) или None."""
    if not (hotel.complete and building.complete):
        return None
    if hotel.street_type and building.street_type and hotel.street_type != building.street_type:
        return None  # «Смоленская улица» и «Смоленская площадь» — разные адреса
    if hotel.street_key == building.street_key:
        street = "same"
    elif SequenceMatcher(None, hotel.street_key, building.street_key).ratio() >= SIMILAR_STREET:
        street = "similar"
    else:
        return None
    if hotel.house_key == building.house_key:
        house = "exact"
    elif _base(hotel.house) == _base(building.house):
        house = "partial"  # тот же номер, но корпус, строение или литера расходятся
    else:
        return None
    return {"street": street, "house": house}


class BuildingIndex:
    """Здания по ключу улицы — чтобы не сравнивать каждый отель с сотнями тысяч контуров."""

    def __init__(self, buildings: Iterable[Building]) -> None:
        self.by_street: dict[str, list[tuple[Building, Address]]] = defaultdict(list)
        self.buildings = list(buildings)
        for b in self.buildings:
            for a in b.addresses:
                if a.street_key:
                    self.by_street[a.street_key].append((b, a))

    def candidates(self, hotel: Address) -> list[tuple[Building, Address, dict[str, str]]]:
        key = hotel.street_key
        if not key:
            return []
        keys = [key] if key in self.by_street else []
        keys += [
            k
            for k in self.by_street
            if k != key
            and k[:2] == key[:2]
            and abs(len(k) - len(key)) <= 2
            and SequenceMatcher(None, k, key).ratio() >= SIMILAR_STREET
        ]
        out = []
        for k in keys:
            for b, a in self.by_street[k]:
                if m := compare(hotel, a):
                    out.append((b, a, m))
        return out

    def nearest(self, lat: float, lon: float, n: int = 3) -> list[tuple[Building, float]]:
        """Ближайшие здания с адресом — подсказка человеку, когда адрес не совпал."""
        near = [  # грубый отсев по первой вершине (~1 км), чтобы не мерить весь город
            b
            for b in self.buildings
            if b.addresses
            and b.rings
            and abs(b.rings[0][0][1] - lat) < 0.01
            and abs(b.rings[0][0][0] - lon) < 0.015
        ]
        ranked = sorted(((b, distance_m(lat, lon, b)) for b in near), key=lambda t: t[1])
        return ranked[:n]


def _confidence(level: str, d: float, n_best: int) -> str:
    if n_best > 1:
        return "ambiguous"
    if level == "exact" and d <= NEAR_M:
        return "high"
    if d > FAR_M:
        return "low"
    return "medium"


def propose(
    hotel: dict[str, Any],
    index: BuildingIndex,
    *,
    address: Address | None = None,
    proposed_by: str = "script:agents.address",
    v9_links: Iterable[str] = (),
) -> dict[str, Any]:
    """Строка `curation_item` (core/db.py, kind = match_hotel_building) для одного отеля."""
    addr = address or parse_address(hotel.get("address") or "")
    lat, lon = float(hotel["latitude"]), float(hotel["longitude"])
    evidence: list[dict[str, Any]] = [
        {
            "kind": "hotel_address",
            "source": "ostrovok",
            "raw": addr.raw,
            "normalized": addr.text(),
            "street_key": addr.street_key,
            "house_key": addr.house_key,
            "dropped": list(addr.dropped),
        }
    ]
    links = sorted(set(v9_links))
    if links:
        evidence.append({"kind": "v9_link", "building_ids": links})
    found = [(b, a, m, distance_m(lat, lon, b)) for b, a, m in index.candidates(addr)]
    exact = [c for c in found if c[2] == {"street": "same", "house": "exact"}]
    best = sorted(exact or found, key=lambda c: c[3])
    if not best:
        reason = "address_incomplete" if not addr.complete else "address_not_matched"
        nearest = [
            {"building_id": b.id, "address": b.addresses[0].text(), "distance_m": round(d)}
            for b, d in index.nearest(lat, lon)
        ]
        evidence.append({"kind": "nearest_buildings", "note": "не кандидаты", "items": nearest})
        return _item(hotel, None, "none", reason, evidence, proposed_by, addr)
    # Несколько контуров с тем же адресом рядом (части здания) — человеку выбрать или объединить.
    top = best[0]
    same_level = [c for c in best if c[2] == top[2] and c[3] <= max(top[3], NEAR_M)]
    confidence = _confidence(
        "exact" if top[2] == {"street": "same", "house": "exact"} else "partial",
        top[3],
        len({c[0].id for c in same_level}),
    )
    for b, a, m, d in best[:5]:
        evidence.append(
            {
                "kind": "building_address",
                "building_id": b.id,
                "source": "osm",
                "tags": {k: v for k, v in b.tags.items() if k.startswith(("addr", "building"))},
                "normalized": a.text(),
                "street_match": m["street"],
                "house_match": m["house"],
                "distance_m": round(d, 1),
            }
        )
    reason = f"street_{top[2]['street']}+house_{top[2]['house']}"
    if top[3] > FAR_M:
        reason += "+far_from_ostrovok_point"
    alternatives = [c[0].id for c in best[1:5]]
    return _item(hotel, top[0].id, confidence, reason, evidence, proposed_by, addr, alternatives)


def _item(
    hotel: dict[str, Any],
    building_id: str | None,
    confidence: str,
    reason: str,
    evidence: list[dict[str, Any]],
    proposed_by: str,
    addr: Address,
    alternatives: list[str] | None = None,
) -> dict[str, Any]:
    note = {
        "high": f"Совпал адрес: {addr.text()}",
        "medium": f"Адрес совпал не полностью или здание далеко: {addr.text()}",
        "ambiguous": "Несколько зданий с этим адресом — выбрать или объединить",
        "low": "Адрес совпал, но здание далеко от точки Островка — проверить точку отеля",
        "none": "Адрес не разобран полностью"
        if reason == "address_incomplete"
        else "Здание с этим адресом не найдено",
    }[confidence]
    return {
        "city_id": hotel.get("city_id"),
        "kind": "match_hotel_building",
        "subject_level": "hotel",
        "subject_id": str(hotel["id"]),
        "feature_id": None,
        "proposal": {
            "building_id": building_id,
            "confidence": confidence,
            "reason": reason,
            "alternatives": alternatives or [],
        },
        "proposed_by": proposed_by,
        "evidence": evidence,
        "state": "proposed",
        "estimated_review_minutes": REVIEW_MINUTES[confidence],
        "note": note,
    }
