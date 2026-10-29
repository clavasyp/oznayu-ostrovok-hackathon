"""Один отель в готовый пакет города — без пересборки города.

Отель, которого нет в снимке Островка 24.10 (страница появилась позже, отель прислал ссылку),
проходит те же шаги, что весь город, на пакете из одного этого отеля:

    python -m pipeline.add_hotel stage --page <страница.html> --url <адрес страницы> \\
        --fetched-at <время загрузки> --city moscow --districts-raw <папка> --work <папка>
    python -m pipeline.add_hotel layers --work <папка> --pbf <срез Geofabrik> \\
        --districts-raw <папка> --heritage-raw <папка> --era5 <кеш> --worldcover-dir <тайлы>
    python -m pipeline.build_view_sun --city moscow --pack <папка>/packs/moscow   # окна
    python -m pipeline.build_view_sun --city moscow --pack <папка>/packs/moscow --sun-hours
    python -m pipeline.add_hotel merge --work <папка> --city moscow               # в packs/

- **stage** — снимок из одной страницы в формате снимка Островка (`raw/`, `next/`,
  `manifests/pages.jsonl`) и пакет: отель, категории, заявления Островка
  (`connectors.ostrovok_site.parse_page`), вопросы отельеру по шаблонам. Город проверяется
  полигоном района OSM (тот же `districts.geojson`, что у `build_districts`).
- **layers** — те же сборщики, что у города: район, корпус по точке с фасадами и H03 / N02 / N03,
  год постройки, охранный статус, стройка, климат и зелень, удобства отеля.
- **merge** — каждый файл пакета из одного отеля переносится в пакет города слоем
  `data/added_<masterId>_<имя>` (`core.packs.add_layer`); станции рядом — по станциям пакета
  города, как `nearby_tables`. Файлы города не меняются, хеши и версия пакета пересчитываются.
  Отель уже в пакете — ошибка.

Не считается: пешие маршруты по графу улиц (W…, `calculate_moscow`) — нужен граф всего города.
Места рядом считает запрос PostGIS (D41): заранее для отеля их считать не нужно.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from connectors.ostrovok_site import parse_page
from core.packs import PackError, PackSource, add_layer, iter_jsonl, read_manifest, sha256_file
from pipeline.build_packs import coverage, write_pack
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.hotelier_questions import build_questions

VERSION = "add-hotel.1"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
MID = re.compile(r"/mid([0-9]+)/")


def _gz(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, mtime=0))


def city_hotel_ids(pack: Path, skip_prefix: str | None = None) -> set[str]:
    """ID отелей пакета города — по всем файлам отелей манифеста, и сборки, и слоёв."""
    return {
        h["id"]
        for f in read_manifest(pack).files
        if f.kind == "hotels" and not (skip_prefix and f.path.startswith(skip_prefix))
        for h in iter_jsonl(pack / f.path)
    }


def district_of(raw: Path, lon: float, lat: float) -> dict | None:
    """Полигон района OSM, в котором лежит точка (ровно один), — или None."""
    from shapely.geometry import Point, shape

    meta = json.loads((raw / "meta.json").read_text("utf-8"))
    path = raw / "districts.geojson"
    if sha256_file(path) != meta["districts_sha256"]:
        raise PackError("districts.geojson не совпадает с meta.json")
    point = Point(lon, lat)
    hits = [
        f["properties"]
        for f in json.loads(path.read_text("utf-8"))["features"]
        if shape(f["geometry"]).intersects(point)
    ]
    if len(hits) != 1:
        return None
    return {"properties": hits[0], "sha256": meta["districts_sha256"], "date": meta["source_date"]}


def stage(
    page: Path, url: str, fetched_at: datetime, city: str, districts_raw: Path, work: Path
) -> dict:
    """Снимок из одной страницы и пакет из одного отеля в `work`."""
    if fetched_at.tzinfo is None:
        raise ValueError("--fetched-at с часовым поясом")
    mid = MID.search(url)
    if not mid:
        raise ValueError(f"не адрес страницы отеля Островка: {url}")
    if work.exists() and any(work.iterdir()):
        raise ValueError(f"{work}: нужна новая или пустая папка")
    html = page.read_bytes()
    found = NEXT_DATA.search(html.decode("utf-8"))
    if not found:
        raise ValueError("на странице нет __NEXT_DATA__")
    payload = found.group(1).encode("utf-8")
    html_sha = hashlib.sha256(html).hexdigest()
    root = work / f"snapshot_{fetched_at:%Y-%m-%d}_pages"
    entry = {
        "url": url,
        "city": city,
        "url_token": url.split("/")[5],
        "mid_url": mid.group(1),
        "final_url": url,
        "status": 200,
        "fetched_at": fetched_at.isoformat(),
        "location": None,
        "raw_file": f"raw/{html_sha[:2]}/{html_sha}.html.gz",
        "sha256": html_sha,
        "next_file": f"next/{html_sha[:2]}/{html_sha}.json.gz",
        "masterId": mid.group(1),
    }
    _gz(root / entry["raw_file"], html)
    _gz(root / entry["next_file"], payload)
    (root / "manifests").mkdir(parents=True)
    (root / "manifests/pages.jsonl").write_text(json.dumps(entry, ensure_ascii=False) + "\n")

    catalog = parse_page(root, entry)
    hotel = catalog.hotels[0]
    if hotel.id in city_hotel_ids(PACKS / city):
        raise PackError(f"{city}: отель {hotel.id} уже в пакете")
    district = district_of(districts_raw, hotel.longitude, hotel.latitude)
    if district is None:
        raise PackError(f"{hotel.id}: точка не внутри ровно одного района {city} — не наш город")
    hotel = hotel.model_copy(
        update={
            "city_id": city,
            "source": hotel.source
            | {
                "geography_verified": True,
                "source_market": city,
                "geography": {
                    "method": "osm_admin_level_8_polygon",
                    "districts_sha256": district["sha256"],
                    "source_date": district["date"],
                },
                "added_by": VERSION,
            },
        }
    )
    hotels = [hotel.model_dump()]
    facts = []
    for fact in catalog.facts:
        raw = fact.model_dump(mode="json", exclude_none=True)
        raw["city_id"] = city
        facts.append(raw)
    questions, _ = build_questions(hotels, facts, [])
    city_manifest = read_manifest(PACKS / city)
    templates = [
        r
        for f in city_manifest.files
        if f.kind == "coverage"
        for r in iter_jsonl(PACKS / city / f.path)
    ]
    parts = {
        "hotels": hotels,
        "room_types": [r.model_dump() for r in catalog.room_types],
        "facts": facts,
        "questions": questions,
    }
    parts["coverage"] = coverage(hotels, facts, templates)
    write_pack(
        work / "packs",
        city,
        parts,
        sources=[
            PackSource(
                id="ostrovok_site",
                provider="ostrovok.ru",
                release=root.name,
                sha256=sha256_file(root / "manifests/pages.jsonl"),
            )
        ],
        built_at=fetched_at,
        code_version=VERSION,
        notes=f"Один отель со страницы Островка ({url}), {VERSION}.",
    )
    report = {
        "hotel_id": hotel.id,
        "name": hotel.name,
        "address": hotel.address,
        "point": [hotel.latitude, hotel.longitude],
        "district": district["properties"].get("name"),
        "room_types": len(parts["room_types"]),
        "facts": len(facts),
        "questions": len(questions),
    }
    print(json.dumps(report, ensure_ascii=False))
    return report


def layers(
    work: Path,
    city: str,
    pbf: Path,
    districts_raw: Path,
    heritage_raw: Path,
    era5: list[Path],
    worldcover: Path,
) -> None:
    """Сборщики города на пакете из одного отеля: каждый пишет свой слой в этот пакет."""
    from pipeline import (
        build_building_links,
        build_building_years,
        build_construction,
        build_districts,
        build_environment,
        build_heritage,
        build_hotel_amenities,
    )

    packs = work / "packs"
    pack = packs / city
    snapshot = next(work.glob("snapshot_*_pages"))
    if city == "moscow":
        build_districts.apply(districts_raw, pack, areas_file=work / "areas.json")
    build_building_links.extract_pbf(work / "osm_raw", city, pbf, packs=packs)
    print(json.dumps(build_building_links.build_city(work / "osm_raw", city, packs=packs),
                     ensure_ascii=False)[:600])  # fmt: skip
    build_building_years.build(city, packs=packs)
    build_heritage.build(city, heritage_raw, packs=packs)
    build_construction.build(city, pbf, packs=packs)
    build_environment.build(city, era5, worldcover, packs=packs)
    build_hotel_amenities.build(snapshot.resolve(), city, packs=packs)


def station_rows(city: str, hotel: dict) -> list[dict]:
    """Ближайшие станции по видам — правило `nearby_tables` на станциях пакета города."""
    from calculators.route_facades import GEOD
    from pipeline.nearby_tables import STATION_RADIUS_M
    from pipeline.nearby_tables import VERSION as NEARBY

    manifest = read_manifest(PACKS / city)
    stations = [
        r
        for f in manifest.files
        if f.kind == "transit_stations"
        for r in iter_jsonl(PACKS / city / f.path)
    ]
    by_mode: dict[str, list] = {}
    for station in stations:
        distance = GEOD.inv(
            hotel["longitude"], hotel["latitude"], station["longitude"], station["latitude"]
        )[2]
        if distance <= STATION_RADIUS_M:
            by_mode.setdefault(station["mode"], []).append((distance, station))
    rows = []
    for mode, options in sorted(by_mode.items()):
        for rank, (distance, station) in enumerate(sorted(options, key=lambda o: o[0])[:3], 1):
            rows.append(
                {
                    "hotel_id": hotel["id"],
                    "station_id": station["id"],
                    "rank": rank,
                    "distance_m": round(distance, 3),
                    "distance_kind": "geodesic_to_representative_point",
                    "walking_distance_m": None,
                    "source": {
                        "method": NEARBY,
                        "nearest_policy": "3 per mapped mode within 30km by geodesic; "
                        "not nearest by walking",
                        "mode": mode,
                        "dataset_sha256": station["source"].get("dataset_sha256"),
                        "walking_unknown_reason": "entrance_and_connected_path_not_verified",
                    },
                }
            )
    return rows


def merge(work: Path, city: str, packs: Path = PACKS) -> dict:
    """Файлы пакета из одного отеля — слоями в пакет города."""
    small = work / "packs" / city
    manifest = read_manifest(small)
    hotels = [r for f in manifest.files if f.kind == "hotels" for r in iter_jsonl(small / f.path)]
    if len(hotels) != 1:
        raise PackError(f"{small}: нужен пакет из одного отеля, а в нём {len(hotels)}")
    hotel = hotels[0]
    target = packs / city
    prefix = f"data/added_{hotel['id']}_"
    if hotel["id"] in city_hotel_ids(target, skip_prefix=prefix):  # повтор merge — замена слоёв
        raise PackError(f"{city}: отель {hotel['id']} уже в пакете")
    added = {}
    files = [f for f in manifest.files if f.kind != "coverage"]
    files.sort(key=lambda f: f.kind != "hotels")  # отель — первым слоем: на него ссылаются
    for f in files:
        path = prefix + Path(f.path).name
        shutil.copyfile(small / f.path, target / path)
        own = [s for s in manifest.sources if getattr(s, "layer", None) == f.path] or [
            s for s in manifest.sources if not getattr(s, "layer", None)
        ]
        sources = [
            PackSource.model_validate(
                s.model_dump(exclude={"layer"})
                | {"id": f"added_{hotel['id']}_{Path(f.path).stem}:{s.id}"}
            )
            for s in own
        ]
        add_layer(target, path, f.kind, f.rows, sources)
        added[path] = f.rows
    stations = station_rows(city, hotel)
    if stations:
        path = prefix + "hotel_stations.jsonl.gz"
        write_jsonl_gz(target / path, stations)
        add_layer(
            target,
            path,
            "hotel_stations",
            len(stations),
            [
                PackSource(
                    id=f"added_{hotel['id']}_hotel_stations:osm",
                    provider="OpenStreetMap",
                    license="ODbL-1.0",
                    method="nearby-tables.3 (станции пакета города)",
                )
            ],
        )
        added[path] = len(stations)
    report = {
        "hotel_id": hotel["id"],
        "layers": added,
        "pack_version": read_manifest(target).pack_version,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--page", type=Path, required=True, help="сохранённая страница отеля (HTML)")
    s.add_argument("--url", required=True, help="адрес страницы на ostrovok.ru")
    s.add_argument("--fetched-at", required=True, type=datetime.fromisoformat)
    s.add_argument("--districts-raw", type=Path, required=True)
    l_ = sub.add_parser("layers")
    l_.add_argument("--pbf", type=Path, required=True, help="срез Geofabrik города")
    l_.add_argument("--districts-raw", type=Path, required=True)
    l_.add_argument("--heritage-raw", type=Path, required=True)
    l_.add_argument("--era5", type=Path, nargs="+", required=True)
    l_.add_argument("--worldcover-dir", type=Path, required=True)
    m = sub.add_parser("merge")
    for p in (s, l_, m):
        p.add_argument("--work", type=Path, required=True)
        p.add_argument("--city", default="moscow", choices=("moscow",))  # районы — только Москва
    a = ap.parse_args()
    if a.cmd == "stage":
        stage(a.page, a.url, a.fetched_at.astimezone(UTC), a.city, a.districts_raw, a.work)
    elif a.cmd == "layers":
        layers(a.work, a.city, a.pbf, a.districts_raw, a.heritage_raw, a.era5, a.worldcover_dir)
    else:
        merge(a.work, a.city)


if __name__ == "__main__":
    main()
