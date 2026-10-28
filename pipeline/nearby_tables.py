"""D34: места в 1500 м от отелей и ближайшие станции; расстояние WGS84 по прямой.

Пары «отель → место» в пакет не идут (D41): их считает запрос PostGIS по точке места
(`core.geo_store.GeoStore.nearby`). Место попадает в пакет, только если оно нужно словарю мест
(`agents/catalog/places.json`) или характеристике каталога (`core.geo_store.CATALOG_PLACES`:
аптека L01, супермаркет L03, бар N04…). Скамейки, урны, парковки, сами отели — нет.

Протяжённые места (парк — `EDGE_CATEGORIES`) кроме строки объекта с точкой внутри получают
точки края: каждое кольцо полигона — через ≤ `EDGE_SPACING_M` м. Ближайшая к отелю точка
такого места — край парка, а не его середина: километровый парк через дорогу не окажется
«в 500 м». Паспорт точки — `source.geometry` (см. packs/FORMAT.md). Парк без названия меньше
0,5 га (`PARK_MIN_UNNAMED_M2`) — зелень двора, а не парк: категории «парк» у него нет.

Срез Сочи захватывает Абхазию: места и станции вне полигона субъектов РФ (`region.geojson`
среза: Краснодарский край и Адыгея) в пакет не идут — ближайшей станцией не будет «Ԥсоу».

OSM не задаёт время метро/число пересадок. Без датированного ответа провайдера
transit_times остаётся пустой таблицей, запрос отсутствующей пары возвращает unknown.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer
from pyproj.enums import TransformDirection
from scipy.spatial import cKDTree
from shapely import from_wkb
from shapely.geometry import Point, shape
from shapely.prepared import prep

from agents.catalog.places import load_places, matches
from calculators.route_facades import GEOD
from calculators.route_graph import CENTERS, osm_source
from core.geo_store import catalog_place_ids
from core.packs import iter_jsonl

# .2: без пар «отель → место», только места с категорией (D41); .3: точки края парков,
# места и станции за границей страны не берутся
VERSION = "nearby-tables.3"
RADIUS_M = 1500
STATION_RADIUS_M = 30000
PLACE_KEYS = (
    "amenity",
    "shop",
    "tourism",
    "leisure",
    "healthcare",
    "office",
    "aerialway",
    "attraction",
)
EDGE_CATEGORIES = frozenset({"park"})
EDGE_SPACING_M = 150  # расстояние до точки края больше, чем до самого края, не более чем на 75 м
EDGE_METHOD = "edge-points.1"
PARK_MIN_UNNAMED_M2 = 5000


def station_mode(tags: dict) -> str | None:
    if tags.get("disused") == "yes" or tags.get("construction") or tags.get("proposed"):
        return None
    if tags.get("railway") == "tram_stop":
        return "tram"
    if tags.get("railway") in {"station", "halt"} or tags.get("public_transport") == "station":
        if tags.get("station") in {"subway", "light_rail"} or tags.get("subway") == "yes":
            return "metro" if tags.get("station") != "light_rail" else "light_rail"
        if tags.get("train") == "yes" or tags.get("railway") in {"station", "halt"}:
            return "train"
        return "unknown"
    return None


def load_region(osm_dir: Path):
    """Полигон субъектов РФ из среза (`region.geojson`) или None — граница городу не нужна."""
    path = osm_dir / "region.geojson"
    if not path.is_file():
        return None, None
    doc = json.loads(path.read_text("utf-8"))
    geom = shape(doc["union_geometry"])
    if geom.is_empty or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{path}: непригодный полигон субъекта")
    ids = [f["properties"]["osm_id"] for f in doc["features"]]
    return prep(geom), ids


def _lines(geom) -> list:
    """Кольца полигонов (внешние и внутренние) и линии геометрии — по порядку частей."""
    kind = geom.geom_type
    if kind == "Polygon":
        return [geom.exterior, *geom.interiors]
    if kind in {"LineString", "LinearRing"}:
        return [geom]
    if kind in {"MultiPolygon", "MultiLineString", "GeometryCollection"}:
        return [line for part in geom.geoms for line in _lines(part)]
    return []


def edge_points(geom, forward, inverse) -> tuple[list[tuple[float, float]], float]:
    """Точки края через равные отрезки ≤ EDGE_SPACING_M (замкнутое кольцо — без повтора начала,
    линия — с обоими концами) и длина края в метрах. Координаты — 7 знаков, как в OSM."""
    points, total = [], 0.0
    for line in _lines(geom):
        flat = shapely.transform(line, forward)
        length = flat.length
        if not length:
            continue
        total += length
        n = max(1, math.ceil(length / EDGE_SPACING_M))
        closed = line.coords[0] == line.coords[-1]
        for i in range(n if closed else n + 1):
            p = flat.interpolate(length * i / n)
            lon, lat = inverse(p.x, p.y)
            points.append((round(lon, 7), round(lat, 7)))
    return points, total


def build(parts: dict, osm_dir: Path, city: str) -> dict:
    manifest = json.loads((osm_dir / "manifest.json").read_text())
    manifest["sha256"] = manifest["source_sha256"]
    lon, lat = CENTERS[city]
    tr = Transformer.from_crs(
        4326, f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m", always_xy=True
    )

    def forward(xy):  # (N, 2) градусы → метры локальной проекции
        return np.column_stack(tr.transform(xy[:, 0], xy[:, 1]))

    def inverse(x, y):
        return tr.transform(x, y, direction=TransformDirection.INVERSE)

    hotels = sorted(parts["hotels"], key=lambda h: h["id"])
    coords = np.array([[h["longitude"], h["latitude"]] for h in hotels])
    xx, yy = tr.transform(coords[:, 0], coords[:, 1])
    tree = cKDTree(np.column_stack([xx, yy]))

    def hotels_within(x: float, y: float) -> set[int]:
        return {
            i
            for i in tree.query_ball_point(tr.transform(x, y), RADIUS_M * 1.01)
            if GEOD.inv(coords[i, 0], coords[i, 1], x, y)[2] <= RADIUS_M
        }

    region, region_ids = load_region(osm_dir)
    outside: Counter[str] = Counter()
    outside_names: set[tuple[str, str]] = set()
    categories = load_places()
    category_index = {}
    for cid, cat in categories.categories.items():
        for alt in cat.osm:
            for key, values in alt.items():
                for value in values:
                    category_index.setdefault((key, value), set()).add(cid)
    places, stations = {}, {}
    hotels_near = 0
    edge_objects = edge_rows = 0
    for record in iter_jsonl(osm_dir / "records.jsonl.gz"):
        tags = record["tags"]
        mode = station_mode(tags)
        labels = []
        if any(tags.get(k) for k in PLACE_KEYS):
            possible = set()
            for key, value in tags.items():
                possible.update(category_index.get((key, "*"), ()))
                for token in value.split(";"):
                    possible.update(category_index.get((key, token.strip()), ()))
            labels = [cid for cid in possible if matches(tags, categories.categories[cid])]
            labels = sorted(labels + catalog_place_ids(tags))
        # скамейка, урна, парковка, сам отель: ни словарю, ни каталогу, ни станциям не нужно
        if not labels and mode is None:
            continue
        geom = from_wkb(record["geometry_wkb"])
        point = geom.representative_point()
        oid = "osm:" + f"{record['osm_type']}/{record['osm_id']}"
        if region is not None and not region.covers(point):
            # за границей страны (Абхазия у Сочи); в отчёте — то, что иначе попало бы в пакет
            near_outside = bool(labels) and bool(hotels_within(point.x, point.y))
            for kind, wanted in (("places", near_outside), ("stations", mode)):
                if wanted:
                    outside[kind] += 1
                    outside_names.add((kind, f"{tags.get('name') or ''} ({oid})"))
            continue
        source = osm_source(record, manifest)
        if mode:
            stations[oid] = {
                "id": oid,
                "city_id": city,
                "name": tags.get("name"),
                "latitude": point.y,
                "longitude": point.x,
                "mode": mode,
                "source": source
                | {"identity": "OSM object; stop-area grouping not inferred", "station_tags": tags},
            }
        if not labels:
            continue
        area = shapely.transform(geom, forward).area if set(labels) & EDGE_CATEGORIES else 0.0
        if "park" in labels and not tags.get("name") and area < PARK_MIN_UNNAMED_M2:
            labels.remove("park")  # зелень двора без названия, а не парк
            if not labels:
                continue
        near = hotels_within(point.x, point.y)
        edge = sorted(set(labels) & EDGE_CATEGORIES)
        samples, length = [], 0.0
        if edge and geom.geom_type != "Point":
            samples, length = edge_points(geom, forward, inverse)
            samples = [
                (x, y, n)
                for x, y in samples
                if (n := hotels_within(x, y)) and (region is None or region.covers(Point(x, y)))
            ]
        if not near and not samples:
            continue
        hotels_near += len(near.union(*(n for *_, n in samples)))
        row = {
            "id": oid,
            "city_id": city,
            "name": tags.get("name"),
            "latitude": point.y,
            "longitude": point.x,
            "categories": labels,
            "tags": tags,
            "source": source,
        }
        if edge and geom.geom_type != "Point":
            shape_info = {
                "method": EDGE_METHOD,
                "object": oid,
                "spacing_m": EDGE_SPACING_M,
                "edge_length_m": round(length, 1),
                "area_m2": round(area, 1) if area else None,
                "edge_points_near_hotels": len(samples),
            }
            row["source"] = source | {"geometry": shape_info | {"role": "representative_point"}}
            edge_objects += 1
            for k, (x, y, _) in enumerate(samples, 1):
                places[f"{oid}#edge{k:04d}"] = {
                    "id": f"{oid}#edge{k:04d}",
                    "city_id": city,
                    "name": tags.get("name"),
                    "latitude": y,
                    "longitude": x,
                    "categories": edge,
                    "tags": tags,
                    "source": source | {"geometry": shape_info | {"role": "edge"}},
                }
            edge_rows += len(samples)
        places[oid] = row
    station_rows = sorted(stations.values(), key=lambda s: s["id"])
    hotel_stations = []
    for h in hotels:
        by_mode = {}
        for station in station_rows:
            distance = GEOD.inv(
                h["longitude"], h["latitude"], station["longitude"], station["latitude"]
            )[2]
            if distance <= STATION_RADIUS_M:
                by_mode.setdefault(station["mode"], []).append((distance, station["id"]))
        for mode, options in sorted(by_mode.items()):
            for rank, (distance, sid) in enumerate(sorted(options)[:3], 1):
                hotel_stations.append(
                    {
                        "hotel_id": h["id"],
                        "station_id": sid,
                        "rank": rank,
                        "distance_m": round(distance, 3),
                        "distance_kind": "geodesic_to_representative_point",
                        "walking_distance_m": None,
                        "source": {
                            "method": VERSION,
                            "nearest_policy": "3 per mapped mode within 30km by geodesic; "
                            "not nearest by walking",
                            "mode": mode,
                            "dataset_sha256": manifest["sha256"],
                            "walking_unknown_reason": "entrance_and_connected_path_not_verified",
                        },
                    }
                )
    parts["places"] = sorted(places.values(), key=lambda p: p["id"])
    parts["transit_stations"], parts["hotel_stations"] = station_rows, hotel_stations
    parts["transit_times"] = []
    objects = [p for p in parts["places"] if "#edge" not in p["id"]]
    return {
        "provider": "OpenStreetMap",
        "radius_m": RADIUS_M,
        "places": len(objects),
        "place_rows": len(parts["places"]),
        "edge_objects": edge_objects,
        "edge_rows": edge_rows,
        "edge_spacing_m": EDGE_SPACING_M,
        "hotel_place_pairs": hotels_near,  # не в пакете: столько пар даёт запрос PostGIS
        "stations": len(station_rows),
        "hotel_station_pairs": len(hotel_stations),
        "categories": dict(sorted(Counter(c for p in objects for c in p["categories"]).items())),
        "region": region_ids,
        "outside_region": dict(outside),
        "outside_region_names": sorted(name for _, name in outside_names),
        "transit_times": 0,
        "transit_time_status": "unknown",
        "reason": "2gis_key_and_dated_transit_responses_unavailable",
        "distance_kind": "geodesic_to_representative_point",
        "all_recorded_locations_within_radius": True,
        "real_world_completeness_guaranteed": False,
        "walking_access_guaranteed": False,
    }
