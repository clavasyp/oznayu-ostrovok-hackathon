"""Геометрические фасады D39; размещения номеров и физические окна не предполагаются."""

from __future__ import annotations

import hashlib
import json
import math

from pyproj import Geod
from shapely.geometry import LineString, mapping, shape
from shapely.geometry.polygon import orient

GEOD = Geod(ellps="WGS84")
SIDES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
VERSION = "facade-catalog.1"


def facade_rows(building: dict) -> list[dict]:
    """ID зависит от координат сегмента, не от начала/обхода кольца или номера корпуса.

    Нормаль направлена от тела здания: у внутренних колец — в геометрическую пустоту.
    Открытый двор из внешнего контура не выводится: is_courtyard=None.
    """
    geom = shape(building["geometry"])
    if geom.geom_type not in {"Polygon", "MultiPolygon"} or not geom.is_valid:
        raise ValueError("Facade catalog requires a valid polygonal building")
    result = {}
    for polygon in [geom] if geom.geom_type == "Polygon" else geom.geoms:
        polygon = orient(polygon, sign=1)
        for courtyard, ring in [(False, polygon.exterior)] + [(True, r) for r in polygon.interiors]:
            coords = list(ring.coords)
            for a, b in zip(coords, coords[1:], strict=False):
                bearing, _, length = GEOD.inv(*a[:2], *b[:2])
                if length < 0.01:
                    continue
                azimuth = (bearing + 90) % 360
                endpoints = sorted([list(a[:2]), list(b[:2])])
                digest = hashlib.sha256(
                    json.dumps([endpoints, courtyard], separators=(",", ":")).encode()
                ).hexdigest()[:20]
                fid = "seg-" + digest
                source = {
                    "provider": "OpenStreetMap",
                    "url": "https://www.openstreetmap.org/" + building["id"],
                    "license": "ODbL-1.0",
                }
                result[fid] = {
                    "building_id": building["id"],
                    "facade_id": fid,
                    "azimuth_deg": round(azimuth, 6) % 360,
                    "side": SIDES[int(math.floor((azimuth + 22.5) / 45)) % 8],
                    "is_courtyard": True if courtyard else None,
                    "geometry": mapping(LineString(endpoints)),
                    "length_m": length,
                    "source": source
                    | {
                        "method": VERSION,
                        "footprint_sha256": hashlib.sha256(geom.wkb).hexdigest(),
                        "scope": "mapped footprint normal; no existing window or room verified",
                        "courtyard_basis": "interior_ring"
                        if courtyard
                        else "unknown_open_or_closed_courtyard",
                    },
                }
    return [result[k] for k in sorted(result)]


def enrich(parts: dict, source: dict | None = None) -> dict:
    facades = [row for building in parts.get("buildings", []) for row in facade_rows(building)]
    parts["facades"] = facades
    for facade in facades:
        facade["source"].update(source or {})
    for building in parts.get("buildings", []):
        tags = building["tags"]
        tags["placement_catalog"] = {
            "mapped_names": [
                {"name": tags[key], "tag": key, "source": "OpenStreetMap"}
                for key in ("name", "name:ru", "official_name", "alt_name", "ref")
                if tags.get(key)
            ],
            "hotel_aliases_require_evidence": True,
            "building_census_complete": False,
        }
    return {
        "buildings": len(parts.get("buildings", [])),
        "facades": len(facades),
        "inner_ring_facades": sum(f["is_courtyard"] is True for f in facades),
        "actual_windows_or_room_placements_invented": 0,
    }
