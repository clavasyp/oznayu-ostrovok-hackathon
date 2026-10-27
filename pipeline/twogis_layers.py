"""Присоединение нормализованного 2ГИС bundle к OSM; разные источники не склеиваются.

Пары «отель → место» не строятся (D41): места рядом считает запрос PostGIS по точке места.
"""

import json
from datetime import datetime

from connectors.twogis import scrub
from core.packs import PackSource, sha256_file


def merge(parts, path, city):
    bundle = json.loads(path.read_text())
    if (
        bundle.get("format") != "twogis-layers.1"
        or bundle.get("city_id") != city
        or bundle.get("commercial_ok") is not False
    ):
        raise ValueError("Invalid 2GIS bundle format/city/rights marker")
    if scrub(bundle) != bundle:
        raise ValueError("2GIS bundle contains forbidden photo/review/key fields")
    places, times = bundle.get("places", []), bundle.get("transit_times", [])
    retrieved = []
    for row in places + times:
        if (
            row["city_id"] != city
            or row["source"].get("provider") != "2gis"
            or row["source"].get("commercial_ok") is not False
        ):
            raise ValueError("2GIS row requires its own dated passport and commercial_ok=false")
        source = row["source"]
        dates = (
            [source["fetched_at"]]
            if source.get("fetched_at")
            else [page["fetched_at"] for page in source.get("pages", []) if page.get("fetched_at")]
        )
        if not dates or any(datetime.fromisoformat(stamp).tzinfo is None for stamp in dates):
            raise ValueError("2GIS passport requires retrieval dates with timezone")
        retrieved.extend(datetime.fromisoformat(stamp) for stamp in dates)
    place_fields = {
        "id",
        "city_id",
        "name",
        "latitude",
        "longitude",
        "categories",
        "tags",
        "source",
    }
    time_fields = {
        "id",
        "city_id",
        "source_station_id",
        "target_station_id",
        "departure_at",
        "profile",
        "duration_s",
        "transfers",
        "source",
    }
    if any(set(row) != place_fields for row in places) or any(
        set(row) != time_fields for row in times
    ):
        raise ValueError("2GIS bundle must contain normalized rows, not raw API objects")
    if any(
        not row["id"].startswith("2gis:")
        or not -180 <= row["longitude"] <= 180
        or not -90 <= row["latitude"] <= 90
        for row in places
    ):
        raise ValueError("2GIS place requires provider ID and valid coordinates")
    existing = {r["id"] for r in parts["places"]}
    if len({r["id"] for r in places}) != len(places) or any(p["id"] in existing for p in places):
        raise ValueError("Duplicate 2GIS place ID")
    parts["places"].extend(places)
    parts["transit_times"].extend(times)
    return PackSource(
        id="2gis",
        provider="2gis",
        sha256=sha256_file(path),
        license="D37 hackathon use",
        commercial_ok=False,
        fetched_at=max(retrieved) if retrieved else None,
        collection=bundle.get("collection", {}),
    )
