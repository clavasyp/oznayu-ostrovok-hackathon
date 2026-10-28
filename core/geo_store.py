"""Чтение подготовленных D34/D39 без графов, сети и зависимостей builder."""

import json
import re
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import cast, func, literal, select, true
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
# Парк в пакете — строка объекта и точки его края (`source.geometry`, pipeline/nearby_tables.py):
# ближайшая из них — расстояние до края парка по прямой; в списке мест парк — одной строкой.
PLACE_OBJECT = func.coalesce(Place.source["geometry"]["object"].astext, Place.id)


def distance_kind(source: dict | None) -> str:
    """Точка края протяжённого места (парк) — до края; иначе — до точки объекта."""
    role = ((source or {}).get("geometry") or {}).get("role")
    return "geodesic_to_edge_point" if role == "edge" else "geodesic_to_representative_point"


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


# Цель поездки — скорее достопримечательность, чем магазин с тем же словом в названии.
SIGHTS = frozenset({"theatre", "museum", "park", "zoo", "theme_park", "viewpoint", "church",
                    "cinema", "water_park", "ski_lift", "mall", "market"})  # fmt: skip
_TARGET_STOP = frozenset({"до", "в", "на", "к", "и", "от", "с", "со", "по", "у"})


_VOWELS = set("аеёиоуыэюяйь")


def _stem(word: str) -> str:
    """Основа без падежного окончания: «большого» → «большо», «розы» → «роз», «театр» → «театр»."""
    if len(word) > 5:
        return word[:-2]
    if len(word) > 3 and word[-1] in _VOWELS:
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))


def target_stems(text: str) -> list[str]:
    """Основы слов цели: «Большого театра» → «большо», «теат»."""
    return [_stem(w) for w in _words(text) if w not in _TARGET_STOP]


def target_match(name: str, stems: list[str]) -> bool:
    """Каждая основа гостя — основа какого-то слова названия (одна — начало другой, разница не
    больше трёх букв): «Красной площади» ≠ «Красноказарменная площадь»."""
    names = [_stem(w) for w in _words(name)]

    def same(a: str, b: str) -> bool:
        short, long = sorted((a, b), key=len)
        return len(short) >= 3 and long.startswith(short) and len(long) - len(short) <= 3

    return all(any(same(st, n) for n in names) for st in stems)


def target_rank(name: str, categories: list[str] | None, n_stems: int) -> tuple[int, int, str]:
    """Чем меньше: достопримечательность, меньше лишних слов, название по алфавиту."""
    extra = len(_words(name)) - n_stems
    return (0 if SIGHTS & set(categories or []) else 1, max(extra, 0), name)


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

    def find_target(self, city_id: str, text: str) -> dict | None:
        """Цель «доехать до X» по названию среди мест и станций города: все основы слов гостя —
        в названии («Большого театра» → «Большой театр», «Розы Хутор» → станция «Роза Хутор»).
        Из подходящих — достопримечательность, а не сувенирная лавка с тем же словом, и название
        без лишних слов."""
        stems = target_stems(text)
        if not stems:
            return None
        with Session(self.engine) as session:
            places = session.execute(
                select(Place.id, Place.name, Place.categories, Place.latitude, Place.longitude)
                .where(
                    Place.city_id == city_id,
                    Place.name.is_not(None),
                    ~Place.id.contains("#edge"),
                    *(Place.name.ilike(f"%{stem[:4]}%") for stem in stems),
                )
                .limit(500)
            ).all()
            stations = session.execute(
                select(
                    TransitStation.id,
                    TransitStation.name,
                    TransitStation.mode,
                    TransitStation.latitude,
                    TransitStation.longitude,
                )
                .where(
                    TransitStation.city_id == city_id,
                    *(TransitStation.name.ilike(f"%{stem[:4]}%") for stem in stems),
                )
                .limit(50)
            ).all()
        found = [
            {"place_id": r.id, "name": r.name, "categories": r.categories,
             "latitude": r.latitude, "longitude": r.longitude}
            for r in places
        ] + [
            {"place_id": r.id, "name": r.name, "categories": [f"station:{r.mode}"],
             "latitude": r.latitude, "longitude": r.longitude}
            for r in stations
        ]  # fmt: skip
        found = [t for t in found if target_match(t["name"], stems)]
        if not found:
            return None
        return min(found, key=lambda t: target_rank(t["name"], t["categories"], len(stems)))

    def distances(self, hotel_ids: list[str], target: dict) -> dict[str, int]:
        """Расстояние по прямой (сфероид WGS84) от каждого отеля до точки цели, м."""
        if not hotel_ids:
            return {}
        point = cast(
            func.ST_SetSRID(func.ST_MakePoint(target["longitude"], target["latitude"]), 4326),
            Geography,
        )
        with Session(self.engine) as session:
            rows = session.execute(
                select(Hotel.id, func.ST_Distance(cast(Hotel.geom, Geography), point)).where(
                    Hotel.id.in_(hotel_ids)
                )
            ).all()
        return {hid: round(float(d)) for hid, d in rows if d is not None}

    def nearest(
        self, hotel_ids: list[str], category: str, radius_m: float
    ) -> tuple[dict[str, dict], int]:
        """Ближайшее место категории в радиусе от каждого отеля — один запрос на весь набор
        (поиск «рядом место типа X», D34). Расстояние — по прямой, по сфероиду WGS84, как в
        `nearby`; у парка — до ближайшей точки края. Свои корпуса отеля не в счёт. Второе
        значение — сколько строк мест этой категории в городах отелей: 0 — данных о таких
        местах нет вовсе, а не «рядом нет»."""
        if not hotel_ids:
            return {}, 0
        hotel = (
            select(Hotel.id.label("hid"), cast(Hotel.geom, Geography).label("pt"))
            .where(Hotel.id.in_(hotel_ids))
            .subquery()
        )
        own = select(literal("osm:") + HotelBuilding.building_id).where(
            HotelBuilding.hotel_id == hotel.c.hid
        )
        distance = func.ST_Distance(Place.geog, hotel.c.pt)
        near = (
            select(Place.id, Place.name, Place.source, distance.label("distance_m"))
            .where(
                Place.categories.contains([category]),
                func.ST_DWithin(Place.geog, hotel.c.pt, radius_m),
                Place.id.not_in(own),
            )
            .order_by(distance, Place.id)
            .limit(1)
            .lateral()
        )
        cities = select(Hotel.city_id).where(Hotel.id.in_(hotel_ids)).distinct()
        with Session(self.engine) as session:
            rows = session.execute(
                select(hotel.c.hid, near).select_from(hotel.join(near, true()))
            ).all()
            known = session.scalar(
                select(func.count())
                .select_from(Place)
                .where(Place.city_id.in_(cities), Place.categories.contains([category]))
            )
        out = {}
        for row in rows:
            source = {"method": NEARBY_METHOD, "passport_ref": "place.source"}
            if (row.source or {}).get("provider") == "2gis":
                source |= {"provider": "2gis", "commercial_ok": False}  # D37
            out[row.hid] = {
                "place_id": row.id,
                "name": row.name,
                "distance_m": round(float(row.distance_m)),
                "distance_kind": distance_kind(row.source),
                "place_source": row.source,
                "source": source,
            }
        return out, int(known or 0)

    def place_counts(
        self, hotel_ids: list[str], category: str, radius_m: float
    ) -> tuple[dict[str, dict], int]:
        """Сколько мест категории в радиусе от каждого отеля и до ближайшего — один запрос
        (калькулятор `geo.place_count`). Отели без мест в радиусе — `{count: 0}`. Второе
        значение — сколько мест категории в городах отелей (0 — данных о таких местах нет)."""
        if not hotel_ids:
            return {}, 0
        hotel = (
            select(Hotel.id.label("hid"), cast(Hotel.geom, Geography).label("pt"))
            .where(Hotel.id.in_(hotel_ids))
            .subquery()
        )
        own = select(literal("osm:") + HotelBuilding.building_id).where(
            HotelBuilding.hotel_id == hotel.c.hid
        )
        distance = func.ST_Distance(Place.geog, hotel.c.pt)
        near = (
            select(func.count().label("n"), func.min(distance).label("nearest_m"))
            .where(
                Place.categories.contains([category]),
                func.ST_DWithin(Place.geog, hotel.c.pt, radius_m),
                Place.id.not_in(own),
            )
            .lateral()
        )
        cities = select(Hotel.city_id).where(Hotel.id.in_(hotel_ids)).distinct()
        with Session(self.engine) as session:
            rows = session.execute(
                select(hotel.c.hid, near.c.n, near.c.nearest_m).select_from(
                    hotel.join(near, true())
                )
            ).all()
            known = session.scalar(
                select(func.count())
                .select_from(Place)
                .where(Place.city_id.in_(cities), Place.categories.contains([category]))
            )
        out = {
            row.hid: {
                "count": int(row.n),
                "nearest_m": None if row.nearest_m is None else round(float(row.nearest_m)),
            }
            for row in rows
        }
        return out, int(known or 0)

    def nearby(
        self, hotel_id: str, category: str | None, radius_m: float, limit: int, offset: int
    ) -> dict:
        """Места в радиусе от точки отеля: ST_DWithin по индексу GiST и ST_Distance по сфероиду
        WGS84 — то же расстояние по прямой, что считала сборка пакета. Сам отель — его
        корпуса из `hotel_building` — в выдачу не попадает. Парк — одной строкой: ближайшая
        к отелю из его точек (край или точка объекта); `total` — число мест, а не точек."""
        point = select(cast(Hotel.geom, Geography)).where(Hotel.id == hotel_id).scalar_subquery()
        own = select(literal("osm:") + HotelBuilding.building_id).where(
            HotelBuilding.hotel_id == hotel_id
        )
        distance = func.ST_Distance(Place.geog, point)
        where = [func.ST_DWithin(Place.geog, point, radius_m), Place.id.not_in(own)]
        if category:
            where.append(Place.categories.contains([category]))
        nth = func.row_number().over(partition_by=PLACE_OBJECT, order_by=(distance, Place.id))
        ranked = (
            select(*PLACE_COLUMNS, distance.label("distance_m"), nth.label("nth"))
            .where(*where)
            .subquery()
        )
        nearest = select(ranked).where(ranked.c.nth == 1).subquery()
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(nearest))
            rows = session.execute(
                select(nearest)
                .order_by(nearest.c.distance_m, nearest.c.id)
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
                "distance_kind": distance_kind(place["source"]),
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
