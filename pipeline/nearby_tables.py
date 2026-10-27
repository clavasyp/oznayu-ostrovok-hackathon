"""D34: места в 1500 м от отелей и ближайшие станции; расстояние WGS84 по прямой.

Пары «отель → место» в пакет не идут (D41): их считает запрос PostGIS по точке места
(`core.geo_store.GeoStore.nearby`). Место попадает в пакет, только если оно нужно словарю мест
(`agents/catalog/places.json`) или характеристике каталога (`core.geo_store.CATALOG_PLACES`:
аптека L01, супермаркет L03, бар N04…). Скамейки, урны, парковки, сами отели — нет.

OSM не задаёт время метро/число пересадок. Без датированного ответа провайдера
transit_times остаётся пустой таблицей, запрос отсутствующей пары возвращает unknown.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely import from_wkb

from agents.catalog.places import load_places, matches
from calculators.route_facades import GEOD
from calculators.route_graph import CENTERS, osm_source
from core.geo_store import catalog_place_ids
from core.packs import iter_jsonl

VERSION = "nearby-tables.2"  # .2: без пар «отель → место», только места с категорией (D41)
RADIUS_M = 1500
STATION_RADIUS_M = 30000


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


def build(parts: dict, osm_dir: Path, city: str) -> dict:
    manifest = json.loads((osm_dir / "manifest.json").read_text())
    manifest["sha256"] = manifest["source_sha256"]
    lon, lat = CENTERS[city]
    tr = Transformer.from_crs(
        4326, f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m", always_xy=True
    )
    hotels = sorted(parts["hotels"], key=lambda h: h["id"])
    coords = np.array([[h["longitude"], h["latitude"]] for h in hotels])
    xx, yy = tr.transform(coords[:, 0], coords[:, 1])
    tree = cKDTree(np.column_stack([xx, yy]))
    categories = load_places()
    category_index = {}
    for cid, cat in categories.categories.items():
        for alt in cat.osm:
            for key, values in alt.items():
                for value in values:
                    category_index.setdefault((key, value), set()).add(cid)
    places, stations = {}, {}
    hotels_near = 0
    for record in iter_jsonl(osm_dir / "records.jsonl.gz"):
        tags = record["tags"]
        mode = station_mode(tags)
        is_place = any(
            tags.get(k)
            for k in (
                "amenity",
                "shop",
                "tourism",
                "leisure",
                "healthcare",
                "office",
                "aerialway",
                "attraction",
            )
        )
        if not is_place and mode is None:
            continue
        point = from_wkb(record["geometry_wkb"]).representative_point()
        source = osm_source(record, manifest)
        oid = "osm:" + f"{record['osm_type']}/{record['osm_id']}"
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
        if not is_place:
            continue
        possible = set()
        for key, value in tags.items():
            possible.update(category_index.get((key, "*"), ()))
            for token in value.split(";"):
                possible.update(category_index.get((key, token.strip()), ()))
        labels = [cid for cid in possible if matches(tags, categories.categories[cid])]
        labels = sorted(labels + catalog_place_ids(tags))
        if not labels:  # скамейка, урна, парковка, сам отель: ни словарю, ни каталогу не нужно
            continue
        near = 0
        for i in tree.query_ball_point(tr.transform(point.x, point.y), RADIUS_M * 1.01):
            if GEOD.inv(coords[i, 0], coords[i, 1], point.x, point.y)[2] <= RADIUS_M:
                near += 1
        if not near:
            continue
        hotels_near += near
        places[oid] = {
            "id": oid,
            "city_id": city,
            "name": tags.get("name"),
            "latitude": point.y,
            "longitude": point.x,
            "categories": labels,
            "tags": tags,
            "source": source,
        }
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
    parts["places"] = list(places.values())
    parts["transit_stations"], parts["hotel_stations"] = station_rows, hotel_stations
    parts["transit_times"] = []
    return {
        "provider": "OpenStreetMap",
        "radius_m": RADIUS_M,
        "places": len(places),
        "hotel_place_pairs": hotels_near,  # не в пакете: столько пар даёт запрос PostGIS
        "stations": len(station_rows),
        "hotel_station_pairs": len(hotel_stations),
        "categories": dict(Counter(c for p in places.values() for c in p["categories"])),
        "transit_times": 0,
        "transit_time_status": "unknown",
        "reason": "2gis_key_and_dated_transit_responses_unavailable",
        "distance_kind": "geodesic_to_representative_point",
        "all_recorded_locations_within_radius": True,
        "real_world_completeness_guaranteed": False,
        "walking_access_guaranteed": False,
    }
