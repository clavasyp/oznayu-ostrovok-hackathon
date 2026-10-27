"""D37: сбор 2ГИС по сетке в отдельный bundle; по умолчанию только план без сети."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from connectors.twogis import (
    DEFAULT_CACHE,
    TwoGISClient,
    grid_plan,
    load_key,
    place_rows,
    transit_rows,
    units,
)
from core.packs import sha256_file
from pipeline.build_packs import new_output, pack_parts


def collect(
    pack: Path,
    mapping: Path,
    cache: Path,
    out: Path,
    *,
    execute=False,
    subscription=False,
    unit_limit=20,
    max_pages=2,
    transit_requests: Path | None = None,
    key: str | None = None,
) -> dict:
    parts = pack_parts(pack)
    city = parts["hotels"][0]["city_id"]
    rubrics = json.loads(mapping.read_text())
    if not rubrics.get("region_id") or not rubrics.get("rubric_categories"):
        raise ValueError("Region ID and verified rubric_categories are required")
    if max_pages < 1 or (not subscription and max_pages > 2):
        raise ValueError("Invalid page limit; demo permits at most two")
    # Все параметры проверяем до первого расходуемого запроса.
    requests = list(json.loads(transit_requests.read_text())) if transit_requests else []
    stations = {s["id"]: s for s in parts.get("transit_stations", [])}
    if not subscription and len(requests) > 2:
        raise ValueError("Demo permits at most two transit checks")
    for request in requests:
        units("transit", request["params"])
        for key, sid in (
            ("source", request["source_station_id"]),
            ("target", request["target_station_id"]),
        ):
            point = request["params"][key]["point"]
            if (
                sid not in stations
                or abs(point["lon"] - stations[sid]["longitude"]) > 1e-7
                or abs(point["lat"] - stations[sid]["latitude"]) > 1e-7
            ):
                raise ValueError("Transit station ID/coordinates mismatch")
    grid = grid_plan(parts["hotels"])
    plan = {
        "city_id": city,
        "grid_cells": len(grid),
        "minimum_places_units": len(grid),
        "max_pages_per_cell": max_pages,
        "subscription": subscription,
        "demo_cells": min(2, len(grid)),
        "live_requests": execute,
        "transit_pairs": len(requests),
        "local_monthly_unit_limit": unit_limit,
    }
    if not execute:
        return plan
    out = new_output(out, pack, cache, mapping)
    # key=None — из окружения; пустой ключ отдаёт сохранённые ответы, новых запросов нет.
    client = TwoGISClient(cache, key=key, subscription=subscription, limit=unit_limit)
    points = grid if subscription else grid[:2]
    places, all_complete = {}, True
    for cell in points:
        response = client.places(
            cell
            | {
                "region_id": rubrics["region_id"],
                "rubric_id": ",".join(sorted(rubrics["rubric_categories"])),
            },
            max_pages=max_pages,
        )
        for row in place_rows(response, city, rubrics["rubric_categories"]):
            places[row["id"]] = row
        all_complete &= response["complete"]
    times = []
    for request in requests:
        a, b = request["source_station_id"], request["target_station_id"]
        response = client.request("transit", request["params"])
        times.extend(transit_rows(response, city=city, source_id=a, target_id=b))
    bundle = {
        "format": "twogis-layers.1",
        "city_id": city,
        "places": list(places.values()),
        "transit_times": times,
        "commercial_ok": False,
        "collection": plan
        | {
            "complete": subscription and all_complete,
            "rubric_mapping_sha256": sha256_file(mapping),
        },
    }
    out.mkdir(parents=True)
    (out / "twogis.json").write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True) + "\n")
    return plan | {
        "places": len(places),
        "transit_times": len(times),
        "complete": bundle["collection"]["complete"],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", required=True, type=Path)
    p.add_argument("--rubrics", required=True, type=Path)
    p.add_argument("--cache", default=DEFAULT_CACHE, type=Path)
    p.add_argument("--env-file", default=Path(".env"), type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--subscription", action="store_true")
    p.add_argument("--monthly-unit-limit", type=int, default=20)
    p.add_argument("--max-pages", type=int, default=2)
    p.add_argument("--transit-requests", type=Path)
    a = p.parse_args()
    key = None
    if a.execute:
        # Ключ читается только для --execute, как в pipeline.check_twogis.
        try:
            key, _ = load_key(a.env_file)
        except (OSError, ValueError):
            p.error("не удалось прочитать TWOGIS_API_KEY из файла окружения")
    print(
        json.dumps(
            collect(
                a.pack,
                a.rubrics,
                a.cache,
                a.out,
                execute=a.execute,
                subscription=a.subscription,
                unit_limit=a.monthly_unit_limit,
                max_pages=a.max_pages,
                transit_requests=a.transit_requests,
                key=key,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
