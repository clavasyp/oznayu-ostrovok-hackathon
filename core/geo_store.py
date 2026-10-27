"""Чтение подготовленных D34/D39 без графов, сети и зависимостей builder."""

import json
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import cast, func, literal, select
from sqlalchemy.orm import Session

from agents.catalog.places import PlaceCategory, load_places, matches
from core.db import (
    Building,
    Facade,
    Hotel,
    HotelBuilding,
    HotelStation,
    Place,
    TransitStation,
    TransitTime,
)
from core.placement_catalog import placement_choices, placement_maps

NEARBY_METHOD = "nearby-postgis.1"  # пары «отель → место» считает запрос, в пакете их нет (D41)
NEARBY_RADIUS_M = 1500
PLACE_COLUMNS = [c for c in Place.__table__.columns if c.name != "geog"]


def _catalog(cid: str, label: str, *osm: dict[str, tuple[str, ...]]) -> PlaceCategory:
    return PlaceCategory(cid, label, (), tuple(osm), {}, (), False)


# Места, которые нужны характеристикам каталога; метки — как в расчёте calculators/route_geo.py.
# В словаре мест (agents/catalog/places.json) их нет: он только про «рядом место типа X» сверх
# каталога. Категория места — id характеристики: `?category=L01` — аптеки рядом.
CATALOG_PLACES = {
    c.id: c
    for c in (
        _catalog("L01", "аптека", {"amenity": ("pharmacy",)}),
        _catalog(
            "L02",
            "кафе, ресторан или бар",
            {"amenity": ("restaurant", "cafe", "fast_food", "food_court", "bar", "pub")},
        ),
        _catalog("L03", "супермаркет", {"shop": ("supermarket",)}),
        _catalog("F01", "детская площадка", {"leisure": ("playground",)}),
        _catalog(
            "F02",
            "детская неотложная помощь",
            {"healthcare:speciality": ("paediatrics", "pediatrics"), "emergency": ("yes",)},
        ),
        _catalog(
            "N04",
            "бар или клуб",
            {"amenity": ("bar", "nightclub", "music_venue")},
            {"club": ("music",)},
        ),
        _catalog("P03", "площадка для выгула собак", {"leisure": ("dog_park",)}),
        _catalog("P04", "ветклиника", {"amenity": ("veterinary",)}),
        _catalog("R04", "коворкинг", {"office": ("coworking",)}, {"amenity": ("coworking_space",)}),
    )
}


def catalog_place_ids(tags: dict[str, str]) -> list[str]:
    """Характеристики каталога, которым нужно место с такими метками OSM."""
    return [cid for cid, category in CATALOG_PLACES.items() if matches(tags, category)]


def place_categories() -> list[str]:
    """Допустимые категории мест: словарь мест и характеристики каталога."""
    return sorted(load_places().categories) + sorted(CATALOG_PLACES)


def row_dict(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class GeoStore:
    def __init__(self, engine):
        self.engine = engine

    def geometry(self, hotel_id: str) -> dict:
        with Session(self.engine) as session:
            links = [
                row_dict(r)
                for r in session.scalars(
                    select(HotelBuilding)
                    .where(HotelBuilding.hotel_id == hotel_id)
                    .order_by(HotelBuilding.building_id)
                )
            ]
            bids = [link["building_id"] for link in links]
            buildings = [
                {"id": r.id, "tags": r.tags, "geometry": json.loads(g)}
                for r, g in session.execute(
                    select(Building, func.ST_AsGeoJSON(Building.geom))
                    .where(Building.id.in_(bids))
                    .order_by(Building.id)
                )
            ]
            facades = [
                row_dict(r)
                for r in session.scalars(
                    select(Facade)
                    .where(Facade.building_id.in_(bids))
                    .order_by(Facade.building_id, Facade.facade_id)
                )
            ]
        aliases, sides = placement_maps(links, facades, hotel_id)
        return {
            "buildings": buildings,
            "hotel_buildings": links,
            "facades": facades,
            "placement_building_aliases": aliases,
            "placement_facades": [
                {"building_id": b, "side": side, "facade_id": fid}
                for (b, side), fid in sorted(sides.items())
            ],
            "placement_building_choices": placement_choices(links, buildings, facades, hotel_id),
            "building_census_complete": False,
            "windows_verified": False,
        }

    def nearby(
        self, hotel_id: str, category: str | None, radius_m: float, limit: int, offset: int
    ) -> dict:
        """Места в радиусе от точки отеля: ST_DWithin по индексу GiST и ST_Distance по сфероиду
        WGS84 — то же расстояние по прямой, что считала сборка пакета. Сам отель — его
        корпуса из `hotel_building` — в выдачу не попадает."""
        point = select(cast(Hotel.geom, Geography)).where(Hotel.id == hotel_id).scalar_subquery()
        own = select(literal("osm:") + HotelBuilding.building_id).where(
            HotelBuilding.hotel_id == hotel_id
        )
        distance = func.ST_Distance(Place.geog, point)
        where = [func.ST_DWithin(Place.geog, point, radius_m), Place.id.not_in(own)]
        if category:
            where.append(Place.categories.contains([category]))
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(Place).where(*where))
            rows = session.execute(
                select(*PLACE_COLUMNS, distance.label("distance_m"))
                .where(*where)
                .order_by(distance, Place.id)
                .offset(offset)
                .limit(limit)
            ).all()
        places = []
        for row in rows:
            place = {c.name: getattr(row, c.name) for c in PLACE_COLUMNS}
            source = {"method": NEARBY_METHOD, "passport_ref": "place.source"}
            if (place["source"] or {}).get("provider") == "2gis":
                source |= {"provider": "2gis", "commercial_ok": False}  # D37
            proximity = {
                "hotel_id": hotel_id,
                "place_id": place["id"],
                "distance_m": round(float(row.distance_m), 3),
                "distance_kind": "geodesic_to_representative_point",
                "walking_distance_m": None,
                "source": source,
            }
            places.append({"place": place, "proximity": proximity})
        return {
            "status": "observed" if total else "unknown",
            "unknown_reason": None if total else "no_mapped_candidates_in_radius_or_layer_missing",
            "places": places,
            "total": total,
            "offset": offset,
            "limit": limit,
            "radius_m": radius_m,
            "distance_kind": "geodesic_to_representative_point",
            "walking_access_verified": False,
            "completeness": "mapped OSM locations of dictionary and catalog categories "
            "in cached extraction; street furniture excluded; no real-world census",
        }

    def stations(self, hotel_id: str) -> dict:
        with Session(self.engine) as session:
            rows = session.execute(
                select(HotelStation, TransitStation)
                .join(TransitStation, TransitStation.id == HotelStation.station_id)
                .where(HotelStation.hotel_id == hotel_id)
                .order_by(TransitStation.mode, HotelStation.rank, TransitStation.id)
            ).all()
            return {
                "status": "observed" if rows else "unknown",
                "unknown_reason": None if rows else "no_mapped_station_candidates_or_layer_missing",
                "stations": [{"station": row_dict(s), "proximity": row_dict(hs)} for hs, s in rows],
                "nearest_policy": "3 per mapped mode within 30km by geodesic",
                "walking_access_verified": False,
            }

    def transit(
        self, city: str, source: str, target: str, departure_at: datetime, profile: str
    ) -> dict:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(TransitTime)
                .where(
                    TransitTime.city_id == city,
                    TransitTime.source_station_id == source,
                    TransitTime.target_station_id == target,
                    TransitTime.departure_at == departure_at,
                    TransitTime.profile == profile,
                )
                .order_by(TransitTime.duration_s, TransitTime.id)
            ).all()
            return {
                "status": "estimate" if rows else "unknown",
                "routes": [row_dict(r) for r in rows],
                "unknown_reason": None
                if rows
                else "no_dated_provider_response_for_this_station_pair_and_profile",
                "departure_at": departure_at.isoformat(),
                "profile": profile,
            }
