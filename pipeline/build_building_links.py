"""Связь «отель → здание» по точке (D42-1) и характеристики корпуса: H03, N02, N03.

Отель без связи с корпусом, чья точка лежит внутри ровно одного контура здания OSM, а адрес
страницы не противоречит адресу здания, получает связь `auto_point` — факт с пометкой метода, не
оценка (ошибочную связь отельер исправит в кабинете). Точка в нескольких контурах или ни в одном,
адрес расходится — связи нет. Прежние связи не меняются.

Факты слоя (`algorithm_version = building-links.1`):
- **H03** — этажность из `building:levels` (как v9: `observed`, тег не целый или его нет →
  «неизвестно»), для каждой связи, где H03 этого корпуса ещё нет — и для прежних связей;
- **N02 / N03** — расстояние от контура корпуса до оси дороги выбранных классов и действующей
  железной дороги, тот же рецепт, что `calculators/route_geo.py` (`N02-R2` / `N03-R2`), для связей
  без такого факта;
- отелю, который так и остался без корпуса, N02 / N03 — **оценка** от точки отеля
  (`N02-R2-point`): ранжирует выдачу, обязательное условие не закрывает (D18).

Два шага; сырьё OSM лежит вне репозитория (ODbL), у каждого файла — источник, время и sha256:

    python -m pipeline.build_building_links extract --raw <папка сырья> \
        --pbf moscow=<срез Geofabrik ЦФО> --pbf sochi=<срез Geofabrik ЮФО>  # PBF пакета
    python -m pipeline.build_building_links build --raw <папка сырья>      # без сети

Вместо `extract` можно `fetch --raw <папка>` — то же сырьё свежим из Overpass (вежливо: запросы по
одному, повтор с паузой по зеркалам); публичные серверы бывают перегружены.

`build` пишет в пакеты Москвы и Сочи слои (`core.packs.add_layer`, packs/FORMAT.md): новые
корпуса, их фасады, связи и факты. Сборка v9 слои сохраняет (`keep_layers`).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.facts import Fact, make_fact_id
from core.packs import (
    Manifest,
    PackError,
    PackSource,
    add_layer,
    iter_jsonl,
    read_manifest,
    sha256_file,
    write_jsonl_gz,
)

VERSION = "building-links.1"
METHOD = "auto_point"  # D42-1: точка отеля внутри ровно одного контура, адрес не противоречит
CITIES = ("moscow", "sochi")
PACKS = Path(__file__).resolve().parents[1] / "packs"

BUILDINGS = "data/buildings_auto_point.jsonl.gz"
FACADES = "data/facades_auto_point.jsonl.gz"
LINKS = "data/hotel_buildings_auto_point.jsonl.gz"
FACTS = "data/building_facts.jsonl.gz"
OWN_LAYERS = (BUILDINGS, FACADES, LINKS, FACTS)

# Как calculators/route_graph.py: классы дорог N02 и действующие пути N03.
BUSY = ("primary", "primary_link", "secondary", "secondary_link", "trunk", "trunk_link")
RAIL = ("rail", "light_rail", "tram")
RAIL_INACTIVE = ("disused", "abandoned", "construction", "proposed")
RAIL_CONVENTION = (
    "rail/light_rail/tram mapped active; disused/abandoned/construction/proposed excluded"
)
CLASS_KEY = {"N02": "road_classes", "N03": "rail_classes"}
CLASSES = {"N02": list(BUSY), "N03": list(RAIL)}
TARGET_TAG = {"N02": "highway", "N03": "railway"}
# Сырьё: запас вокруг отелей — чтобы ближайшая дорога или путь не оказались за краем выборки.
MARGIN_DEG = 0.06
TILE_DEG = 0.35
POINTS_PER_QUERY = 150

OSM_LICENSE = "ODbL-1.0"
LEVELS_CONVENTION = (  # как H03 v9 (research_notes/…/scripts/facades.py)
    "OSM building:levels includes ground floor and excludes basement and roof levels; "
    "mezzanine handling as mapped, not independently inspected"
)
WHY_NO_LINK = {
    "no_footprint": "точка отеля не внутри контура здания на карте",
    "several_footprints": "точка отеля внутри нескольких контуров зданий",
    "address_conflict": "адрес на странице отеля расходится с адресом здания на карте",
    "invalid_footprint": "контур здания на карте некорректен",
}


# ---------------------------------------------------------------------------- сырьё Overpass


ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = "vaibovovka-hotel-search/building-links.1 (Ostrovok hackathon 2026)"


def city_bbox(hotels: list[dict], margin: float = MARGIN_DEG) -> tuple[float, float, float, float]:
    """(юг, запад, север, восток) по точкам отелей с запасом."""
    lats = [float(h["latitude"]) for h in hotels]
    lons = [float(h["longitude"]) for h in hotels]
    return (
        round(min(lats) - margin, 4),
        round(min(lons) - margin, 4),
        round(max(lats) + margin, 4),
        round(max(lons) + margin, 4),
    )


def tiles(bbox: tuple[float, float, float, float], size: float = TILE_DEG) -> list[tuple]:
    s, w, n, e = bbox
    rows, cols = max(1, math.ceil((n - s) / size)), max(1, math.ceil((e - w) / size))
    dy, dx = (n - s) / rows, (e - w) / cols
    return [
        (round(s + i * dy, 5), round(w + j * dx, 5), round(s + (i + 1) * dy, 5),
         round(w + (j + 1) * dx, 5))
        for i in range(rows) for j in range(cols)
    ]  # fmt: skip


def buildings_query(points: list[tuple[float, float]]) -> str:
    """Здания, внутри которых лежит хотя бы одна точка: `is_in` даёт ровно содержащие контуры."""
    inner = "\n".join(f"is_in({lat:.7f},{lon:.7f});" for lat, lon in points)
    return (
        "[out:json][timeout:300];\n(\n" + inner + "\n)->.a;\n"
        '(way(pivot.a)["building"]; relation(pivot.a)["building"];);\nout meta geom;'
    )


def lines_query(kind: str, bbox: tuple) -> str:
    s, w, n, e = bbox
    tag, values = ("highway", BUSY) if kind == "roads" else ("railway", RAIL)
    return (
        f"[out:json][timeout:300][bbox:{s},{w},{n},{e}];\n"
        f'way["{tag}"~"^({"|".join(values)})$"];\nout meta geom;'
    )


def overpass(query: str, endpoints: list[str], attempts: int = 6, pause: float = 15.0):
    """POST к Overpass с повтором: по кругу зеркал, между кругами пауза удваивается.

    Ответ 200 с `remark` об ошибке (тайм-аут, нехватка памяти) — неполный, тоже повтор."""
    import httpx

    delay = pause
    for attempt in range(1, attempts + 1):
        for url in list(endpoints):
            try:
                r = httpx.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=httpx.Timeout(900, connect=30),
                )
            except httpx.HTTPError as e:
                print(f"    {url}: {type(e).__name__}", flush=True)
                continue
            if r.status_code == 200 and r.content.lstrip()[:1] == b"{":
                body = r.json()
                remark = body.get("remark") or ""
                if "error" not in remark and "timed out" not in remark:
                    endpoints.remove(url)
                    endpoints.insert(0, url)  # удачное зеркало — первым в следующий раз
                    return r.content, url, body
                print(f"    {url}: {remark[:120]}", flush=True)
            else:
                print(f"    {url}: HTTP {r.status_code}", flush=True)
        print(f"  попытка {attempt} не удалась, пауза {delay:.0f} с", flush=True)
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("Overpass не ответил ни с одного зеркала")


def fetch_city(raw: Path, city: str, endpoints: list[str], packs: Path = PACKS) -> dict:
    """Скачать сырьё города: здания под точками отелей, дороги N02 и пути N03 по плиткам.

    Каждый ответ — отдельный файл `<вид>_<NNN>.json.gz` как получен, в `manifest.json` — url,
    запрос, время, `timestamp_osm_base` и sha256. Уже скачанный запрос не повторяется."""
    folder = raw / city
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text("utf-8")) if path.is_file() else {"files": []}
    manifest |= {"city_id": city, "license": OSM_LICENSE,
                 "attribution": "© OpenStreetMap contributors"}  # fmt: skip
    done = {f["query_sha256"] for f in manifest["files"]}
    hotels = list(iter_jsonl(packs / city / "data/hotels.jsonl.gz"))
    bbox = city_bbox(hotels)
    manifest["bbox"] = list(bbox)
    points = sorted({(round(float(h["latitude"]), 7), round(float(h["longitude"]), 7))
                     for h in hotels})  # fmt: skip
    jobs = [
        ("buildings", buildings_query(points[i : i + POINTS_PER_QUERY]), None)
        for i in range(0, len(points), POINTS_PER_QUERY)
    ]
    jobs += [(kind, lines_query(kind, t), t) for kind in ("roads", "rail") for t in tiles(bbox)]
    counter = Counter(f["kind"] for f in manifest["files"])
    for k, (kind, query, tile) in enumerate(jobs, 1):
        digest = hashlib.sha256(query.encode()).hexdigest()
        if digest in done:
            continue
        print(f"{city}: запрос {k}/{len(jobs)} ({kind})", flush=True)
        started = datetime.now(UTC)
        content, url, body = overpass(query, endpoints)
        name = f"{kind}_{counter[kind]:03d}.json.gz"
        counter[kind] += 1
        file = folder / name
        with gzip.GzipFile(file, "wb", mtime=0) as gz:
            gz.write(content)
        manifest["files"].append({
            "file": name, "kind": kind, "url": url, "query": query, "query_sha256": digest,
            "bbox": list(tile) if tile else None, "fetched_at": started.isoformat(),
            "osm_base": body.get("osm3s", {}).get("timestamp_osm_base"),
            "elements": len(body.get("elements", [])), "bytes": file.stat().st_size,
            "sha256": sha256_file(file),
        })  # fmt: skip
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
        del content, body
        time.sleep(5)  # вежливо: запросы по одному, с паузой
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


# ------------------------------------------------------------------------- сырьё из среза PBF


GEOFABRIK = {
    "moscow": "https://download.geofabrik.de/russia/central-fed-district-latest.osm.pbf",
    "sochi": "https://download.geofabrik.de/russia/south-fed-district-latest.osm.pbf",
}


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_location(area) -> tuple[float, float] | None:
    for ring in area.outer_rings():
        for node in ring:
            return node.lon, node.lat
    return None


def extract_pbf(raw: Path, city: str, pbf: Path, packs: Path = PACKS) -> dict:
    """Сырьё города из среза Geofabrik (тот же PBF, что у пакета): здания, внутри которых лежит
    точка хотя бы одного отеля, дороги N02 и пути N03 в рамке отелей с запасом (`city_bbox`).

    Формат — как у ответа Overpass (`elements`); у здания контур — `geojson` собранного
    мультиполигона. Координаты узлов — в файловом индексе (sparse_file_array), не в памяти:
    срез ЦФО — около 2 ГБ координат. Индекс удаляется после прохода."""
    import osmium
    from shapely import STRtree, from_wkb, make_valid
    from shapely.geometry import Point, box, mapping

    folder = raw / city
    if (folder / "manifest.json").exists():
        raise PackError(f"{city}: в {folder} уже есть сырьё — для нового среза нужна новая папка")
    folder.mkdir(parents=True, exist_ok=True)
    hotels = list(iter_jsonl(packs / city / "data/hotels.jsonl.gz"))
    s, w, n, e = bbox = city_bbox(hotels)
    envelope = box(w, s, e, n)
    pad = box(w - 0.05, s - 0.05, e + 0.05, n + 0.05)  # первая вершина большого здания
    tree = STRtree([Point(float(h["longitude"]), float(h["latitude"])) for h in hotels])
    factory = osmium.geom.WKBFactory()
    index = folder / "nodes.idx"
    index.unlink(missing_ok=True)
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations(f"sparse_file_array,{index}")
        .with_areas(osmium.filter.KeyFilter("building"))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY | osmium.osm.AREA))
        .with_filter(osmium.filter.KeyFilter("building", "highway", "railway"))
    )
    base = processor.header.get("osmosis_replication_timestamp")
    out: dict[str, list[dict]] = {"buildings": [], "roads": [], "rail": []}
    errors: Counter = Counter()
    started = time.monotonic()
    try:
        for obj in processor:
            try:
                if obj.is_area():
                    tags = dict(obj.tags)
                    first = _first_location(obj)
                    if tags.get("building", "no") == "no" or first is None:
                        continue
                    if not pad.contains(Point(first)):
                        continue
                    geom = from_wkb(factory.create_multipolygon(obj))
                    if not geom.is_valid:
                        geom = make_valid(geom)
                    if not geom.intersects(envelope) or not len(tree.query(geom, "covers")):
                        continue
                    x0, y0, x1, y1 = geom.bounds
                    out["buildings"].append({
                        "type": "way" if obj.from_way() else "relation", "id": obj.orig_id(),
                        "version": obj.version, "timestamp": _iso(obj.timestamp), "tags": tags,
                        "geojson": mapping(geom),
                        "bounds": {"minlat": y0, "minlon": x0, "maxlat": y1, "maxlon": x1},
                    })  # fmt: skip
                elif obj.is_way():
                    t = obj.tags
                    kind = ("roads" if t.get("highway") in BUSY else
                            "rail" if t.get("railway") in RAIL else None)  # fmt: skip
                    if kind is None:
                        continue
                    pts = [(nd.lon, nd.lat) for nd in obj.nodes]
                    if len(pts) < 2 or not envelope.intersects(box(
                        min(p[0] for p in pts), min(p[1] for p in pts),
                        max(p[0] for p in pts), max(p[1] for p in pts),
                    )):  # fmt: skip
                        continue
                    out[kind].append({
                        "type": "way", "id": obj.id, "version": obj.version,
                        "timestamp": _iso(obj.timestamp), "tags": dict(t),
                        "geometry": [{"lat": y, "lon": x} for x, y in pts],
                    })  # fmt: skip
            except (osmium.InvalidLocationError, RuntimeError) as exc:
                errors[type(exc).__name__] += 1
    finally:
        index.unlink(missing_ok=True)
    pbf_sha = sha256_file(pbf)
    fetched = datetime.fromtimestamp(pbf.stat().st_mtime, UTC).isoformat()
    files = []
    for kind, elements in out.items():
        file = folder / f"{kind}_000.json.gz"
        body = {"osm3s": {"timestamp_osm_base": base}, "elements": elements}
        with gzip.GzipFile(file, "wb", mtime=0) as gz:
            gz.write(json.dumps(body, ensure_ascii=False).encode())
        selection = {
            "buildings": "area building!=no, covers a hotel point of the pack",
            "roads": f"way highway in {list(BUSY)}, intersects bbox",
            "rail": f"way railway in {list(RAIL)}, intersects bbox",
        }[kind]  # fmt: skip
        files.append({
            "file": file.name, "kind": kind, "url": GEOFABRIK.get(city), "source_file": pbf.name,
            "source_sha256": pbf_sha, "query": selection, "query_sha256": selection,
            "bbox": list(bbox), "fetched_at": fetched, "osm_base": base,
            "elements": len(elements), "bytes": file.stat().st_size, "sha256": sha256_file(file),
        })  # fmt: skip
    manifest = {
        "city_id": city, "license": OSM_LICENSE, "attribution": "© OpenStreetMap contributors",
        "bbox": list(bbox), "method": f"{VERSION}:pbf", "geometry_diagnostics": dict(errors),
        "seconds": round(time.monotonic() - started, 1), "files": files,
    }  # fmt: skip
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    return manifest


# ------------------------------------------------------------------------------ геометрия OSM


def osm_id(el: dict) -> str:
    return f"{el['type']}/{el['id']}"


def _polygonal(geom):
    """Только площадные части, валидно: MultiPolygon или None."""
    import shapely
    from shapely.geometry import MultiPolygon, Polygon

    if geom is None or geom.is_empty:
        return None
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    parts = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon) and not g.is_empty and g.area > 0:
            parts.append(g)
        elif isinstance(g, MultiPolygon):
            parts.extend(p for p in g.geoms if p.area > 0)
        elif hasattr(g, "geoms"):
            sub = _polygonal(g)
            parts.extend(sub.geoms if sub is not None else [])
    if not parts:
        return None
    out = MultiPolygon(parts)
    return out if out.is_valid else None


def _coords(points: list) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in points if p]


def element_geometry(el: dict):
    """Контур здания из ответа Overpass (`out geom`): way — кольцо, relation — внешние кольца
    минус внутренние. Незамкнутое и вырожденное — None."""
    import shapely
    from shapely.geometry import LineString, Polygon

    if "geojson" in el:  # собранный мультиполигон из среза PBF (`extract_pbf`)
        from shapely.geometry import shape

        return _polygonal(shape(el["geojson"]))
    if el["type"] == "way":
        pts = _coords(el.get("geometry") or [])
        if len(pts) < 4 or pts[0] != pts[-1]:
            return None
        return _polygonal(Polygon(pts))
    if el["type"] != "relation":
        return None
    rings: dict[str, list] = {"outer": [], "inner": []}
    for m in el.get("members") or []:
        pts = _coords(m.get("geometry") or [])
        if m.get("type") == "way" and len(pts) >= 2 and m.get("role") != "part":
            rings["inner" if m.get("role") == "inner" else "outer"].append(LineString(pts))
    if not rings["outer"]:
        return None
    outer = shapely.union_all(list(shapely.polygonize(rings["outer"]).geoms))
    if rings["inner"]:
        inner = shapely.union_all(list(shapely.polygonize(rings["inner"]).geoms))
        outer = outer.difference(inner)
    return _polygonal(outer)


def _round(geojson: Any) -> Any:
    if isinstance(geojson, float):
        return round(geojson, 7)
    if isinstance(geojson, list | tuple):
        return [_round(x) for x in geojson]
    if isinstance(geojson, dict):
        return {k: _round(v) for k, v in geojson.items()}
    return geojson


def building_row(el: dict, geom, city: str) -> dict:
    """Строка `buildings` пакета: id, город, контур (MultiPolygon), теги OSM как есть."""
    from shapely.geometry import mapping, shape

    geojson = mapping(geom)
    rounded = _round(geojson)  # 7 знаков, как в OSM; точки пересечений make_valid — как есть
    return {
        "id": osm_id(el),
        "city_id": city,
        "geometry": rounded if shape(rounded).is_valid else _round_tuples(geojson),
        "tags": dict(el.get("tags") or {}),
    }


def _round_tuples(geojson: Any) -> Any:
    """GeoJSON без округления, кортежи → списки (JSON)."""
    if isinstance(geojson, list | tuple):
        return [_round_tuples(x) for x in geojson]
    if isinstance(geojson, dict):
        return {k: _round_tuples(v) for k, v in geojson.items()}
    return geojson


def osm_timestamp(value: str | None) -> str | None:
    """`2025-02-04T14:48:39Z` → `2025-02-04 14:48:39+00:00`, как в паспортах v9 из PBF."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(sep=" ")


# ----------------------------------------------------------------------------- связь по точке


@dataclass
class Outcome:
    hotel_id: str
    code: str  # linked | no_footprint | several_footprints | address_conflict | …
    candidates: list[str]
    evidence: dict | None = None


def address_check(hotel_address: str, tags: dict) -> tuple[str | None, dict]:
    """Адрес страницы против адресов здания OSM (`addr:*`, `addr2:*`).

    Возвращает (расхождение или None, доказательство). Противоречие — только если каждый адрес
    здания расходится с адресом отеля в том, что названо у обоих (`contradiction`)."""
    from agents.address.match import compare, contradiction, street_known
    from agents.address.normalize import osm_addresses, parse_address

    hotel = parse_address(hotel_address or "")
    osm = osm_addresses({k: str(v) for k, v in tags.items() if isinstance(v, str)})
    evidence = {
        "hotel_address": hotel_address,
        "normalized_hotel": hotel.text(),
        "normalized_osm": [a.text() for a in osm],
    }
    if not osm or not (street_known(hotel) or hotel.house):
        return None, evidence | {"address_check": "not_compared"}
    conflicts = [contradiction(hotel, a) for a in osm]
    if all(conflicts):
        return conflicts[0], evidence | {"address_check": "conflict", "conflict": conflicts[0]}
    full = any(compare(hotel, a) == {"street": "same", "house": "exact"} for a in osm)
    return None, evidence | {"address_check": "match" if full else "compatible"}


def _bounds_box(el: dict):
    """Рамка элемента из ответа Overpass (`bounds`): у контура без площади — всё, что известно."""
    from shapely.geometry import box

    b = el.get("bounds")
    return box(b["minlon"], b["minlat"], b["maxlon"], b["maxlat"]) if b else None


def link_by_point(
    hotels: list[dict], footprints: dict[str, tuple[dict, Any]], skip: set[str]
) -> list[Outcome]:
    """D42-1 для отелей вне `skip` (у них уже есть связь): `footprints` — id → (элемент OSM,
    контур или None). Ровно один содержащий точку контур и непротиворечивый адрес → связь.

    Контур без площади (незамкнутый, вырожденный) не проверить: если точка в его рамке, он
    считается ещё одним кандидатом — связи нет (`invalid_footprint` / `several_footprints`)."""
    from shapely import STRtree
    from shapely.geometry import Point

    ids = sorted(bid for bid, (_, g) in footprints.items() if g is not None)
    tree = STRtree([footprints[b][1] for b in ids]) if ids else None
    broken = {bid: _bounds_box(el) for bid, (el, g) in footprints.items() if g is None}
    out = []
    for h in hotels:
        if h["id"] in skip:
            continue
        point = Point(float(h["longitude"]), float(h["latitude"]))
        inside = (
            [ids[int(i)] for i in tree.query(point, predicate="covered_by")]
            if tree is not None
            else []
        )
        unsure = [bid for bid, box in broken.items() if box is not None and box.covers(point)]
        candidates = sorted(inside + unsure)
        if len(candidates) != 1 or unsure:
            code = ("several_footprints" if len(candidates) > 1 else
                    "invalid_footprint" if unsure else "no_footprint")  # fmt: skip
            out.append(Outcome(h["id"], code, candidates))
            continue
        bid = candidates[0]
        el = footprints[bid][0]
        conflict, evidence = address_check(h.get("address") or "", el.get("tags") or {})
        if conflict:
            out.append(Outcome(h["id"], "address_conflict", candidates, evidence))
            continue
        evidence = {
            "rule": METHOD,
            "decision": "D42-1",
            "point_covered_by_building": True,
            "footprints_containing_point": 1,
            **evidence,
            "source_url": f"https://www.openstreetmap.org/{bid}",
            "osm_version": el.get("version"),
            "osm_timestamp": osm_timestamp(el.get("timestamp")),
        }
        out.append(Outcome(h["id"], "linked", candidates, evidence))
    return out


# --------------------------------------------------------------------------- расстояния N02/N03


class Targets:
    """Оси дорог (N02) или путей (N03) в метрической проекции города, как в route_graph."""

    def __init__(self, city: str, records: list[dict], extent: tuple, raw: dict) -> None:
        from pyproj import CRS, Transformer
        from shapely import STRtree
        from shapely.geometry import LineString, box

        from calculators.route_graph import CENTERS

        s, w, n, e = extent
        x, y = CENTERS.get(city) or ((w + e) / 2, (s + n) / 2)  # проекция — как у route_graph
        self.tr = Transformer.from_crs(
            4326,
            CRS.from_proj4(f"+proj=aeqd +lat_0={y} +lon_0={x} +datum=WGS84 +units=m"),
            always_xy=True,
        )
        self.raw = raw  # паспорт сырья: osm_base, sha256
        self.records, geoms = [], []
        for r in records:
            pts = _coords(r.get("geometry") or [])
            if len(pts) < 2:
                continue
            self.records.append({k: r[k] for k in ("type", "id", "version", "timestamp", "tags")
                                 if k in r})  # fmt: skip
            geoms.append(self.project(LineString(pts)))
        self.geoms = geoms
        self.tree = STRtree(geoms) if geoms else None
        self.extent = self.project(box(w, s, e, n).segmentize(0.005))

    def project(self, geom):
        import shapely

        return shapely.transform(geom, self.tr.transform, interleaved=False)

    def nearest(self, geom_lonlat) -> tuple[dict, float] | None:
        """Ближайшая ось и плановое расстояние, м; None — ближайшая может быть за краем выборки."""
        if self.tree is None:
            return None
        g = self.project(geom_lonlat)
        i = int(self.tree.nearest(g))
        dist = g.distance(self.geoms[i])
        if not self.extent.contains(g) or dist > self.extent.exterior.distance(g):
            return None
        return self.records[i], dist


def active_rail(tags: dict) -> bool:
    """Как route_graph: rail / light_rail / tram без признаков заброшенного или стройки."""
    return (
        tags.get("railway") in RAIL
        and not any(k in tags for k in RAIL_INACTIVE)
        and not any(k.startswith(tuple(p + ":" for p in RAIL_INACTIVE)) for k in tags)
    )


def target_source(r: dict, raw: dict) -> dict:
    """Паспорт объекта OSM — как `calculators.route_graph.osm_source`."""
    return {
        "provider": "OpenStreetMap",
        "url": "https://www.openstreetmap.org/" + osm_id(r),
        "version": r.get("version"),
        "observed_at": osm_timestamp(r.get("timestamp")),
        "date_kind": "OSM edit, not physical inspection",
        "license": "ODbL1.0",
        "license_url": "https://www.openstreetmap.org/copyright",
        "dataset_version": raw.get("osm_base"),
        "dataset_sha256": raw.get("sha256"),
    }


def _attempts(fid: str, rejected: dict[str, str], accepted: str | None) -> list[dict]:
    rows = [{"recipe_id": k, "state": "rejected", "reason": v, "data_kind": "Б"}
            for k, v in rejected.items()]  # fmt: skip
    if accepted:
        rows.append({"recipe_id": accepted, "state": "accepted", "data_kind": "Б"})
    return rows


def fact_row(fact: Fact) -> dict:
    """Факт как в пакетах: пустые поля опускаем, но `value` и `unknown_reason` — всегда."""
    row = fact.model_dump(mode="json", exclude_none=True)
    row.setdefault("value", None)
    row.setdefault("unknown_reason", None)
    return row


def _fact(hotel_id: str, city: str, subject_level: str, subject_id: str, fid: str,
          params: dict, stamp: str, **kw) -> dict:  # fmt: skip
    return fact_row(
        Fact(
            fact_id=make_fact_id(hotel_id, subject_id, fid, params, VERSION),
            hotel_id=hotel_id,
            city_id=city,
            subject_level=subject_level,
            subject_id=subject_id,
            feature_id=fid,
            params=params,
            algorithm_version=VERSION,
            calculated_at=stamp,
            **kw,
        )
    )


NO_OFFICIAL = {
    "N02": "Нет принятого официального слоя дорог для этого корпуса.",
    "N03": "Нет принятого официального слоя действующих железных дорог для этого корпуса.",
}
SCOPE_BUILDING = {
    "N02": "Один корпус отеля; не все корпуса отеля и не расстояние от окна.",
    "N03": "Один корпус отеля; не все корпуса отеля и не расстояние от окна. Действующий путь — "
    "по разметке OSM, а не по расписанию поездов.",
}


def distance_params(fid: str, origin: str) -> dict:
    """Параметры N02 / N03 — как у фактов route-poi.1 (контур) или с точкой отеля (оценка)."""
    params = {CLASS_KEY[fid]: sorted(CLASSES[fid]), "distance_origin": origin,
              "target_geometry": "axis"}  # fmt: skip
    if origin == "accepted building footprint":
        params["rail_status_convention"] = RAIL_CONVENTION if fid == "N03" else None
    elif fid == "N03":
        params["rail_status_convention"] = RAIL_CONVENTION
    return params


def building_distance_fact(
    hotel_id: str, city: str, fid: str, building: dict, footprint, targets: Targets, stamp: str,
    building_file: str,
) -> dict:  # fmt: skip
    """N02 / N03 `computed` от контура корпуса — рецепт `<fid>-R2` route-poi.1."""
    params = distance_params(fid, "accepted building footprint")
    bid = building["id"]
    found = targets.nearest(footprint)
    if found is None:
        return _fact(hotel_id, city, "building", bid, fid, params, stamp, status="unknown",
                     unknown_reason="source_extent_or_matching_source_geometry_unavailable",
                     confidence={"level": "unknown", "reason": "Ближайшая ось может быть за "
                                 "краем выборки OSM."})  # fmt: skip
    r, dist = found
    note = SCOPE_BUILDING[fid]
    return _fact(
        hotel_id, city, "building", bid, fid, params, stamp,
        value={"distance_m": dist, "target_id": osm_id(r),
               "target_class": r["tags"].get(TARGET_TAG[fid]),
               "target_name": r["tags"].get("name"), "building_id": bid},
        unit="m", status="computed", recipe_id=f"{fid}-R2",
        sources=[target_source(r, targets.raw),
                 {"provider": "OpenStreetMap", "url": f"https://www.openstreetmap.org/{bid}",
                  "license": OSM_LICENSE, "snapshot_file": Path(building_file).name}],
        scope_note=note, confidence={"level": "medium", "reason": note},
        recipe_attempts=_attempts(fid, {f"{fid}-R1": NO_OFFICIAL[fid]}, f"{fid}-R2"),
    )  # fmt: skip


def point_estimate_fact(
    hotel: dict, city: str, fid: str, outcome: Outcome, targets: Targets, stamp: str
) -> dict | None:
    """N02 / N03 `estimate` от точки отеля без корпуса (D18): ранжирует, обязательное условие
    не закрывает. Нет оси в выборке — None (останется прежнее «неизвестно»)."""
    from shapely.geometry import Point

    found = targets.nearest(Point(float(hotel["longitude"]), float(hotel["latitude"])))
    if found is None:
        return None
    r, dist = found
    why = WHY_NO_LINK.get(outcome.code, outcome.code)
    note = (
        f"Оценка от точки отеля на карте, а не от контура корпуса: корпус не определён ({why}). "
        "От стены корпуса до дороги может быть ближе. Ранжирует выдачу, обязательное условие "
        "не закрывает (D18)."
    )
    params = distance_params(fid, "OTA hotel point")
    return _fact(
        hotel["id"], city, "hotel", f"hotel:{hotel['id']}", fid, params, stamp,
        value={"distance_m": dist, "target_id": osm_id(r),
               "target_class": r["tags"].get(TARGET_TAG[fid]),
               "target_name": r["tags"].get("name")},
        unit="m", status="estimate", hard_condition_eligible=False, recipe_id=f"{fid}-R2-point",
        sources=[target_source(r, targets.raw)], scope_note=note,
        confidence={"level": "low", "reason": note},
        depends_on=[{"kind": "building_link", "outcome": outcome.code,
                     "candidate_building_ids": outcome.candidates}],
        recipe_attempts=_attempts(fid, {f"{fid}-R1": NO_OFFICIAL[fid],
                                        f"{fid}-R2": f"Корпус отеля не связан: {why}."},
                                  f"{fid}-R2-point"),
    )  # fmt: skip


# -------------------------------------------------------------------------------------- H03


def integer_levels(tags: dict) -> int | None:
    """`building:levels` целым числом 1…180 (как v9); «5;6», «2.5», «3-4» — None."""
    raw = str(tags.get("building:levels") or "").strip().replace(",", ".")
    try:
        n = float(raw)
    except ValueError:
        return None
    return int(n) if n.is_integer() and 0 < n <= 180 else None


def levels_fact(hotel_id: str, city: str, building: dict, link: dict, source: dict,
                stamp: str) -> dict:  # fmt: skip
    """H03 корпуса — формат v9 (`v9.2.facades`): `observed` из тега, иначе «неизвестно»."""
    tags = building.get("tags") or {}
    bid = building["id"]
    params = {
        "building_match_evidence": {
            "method": link.get("method"),
            "rule": (link.get("evidence") or {}).get("rule"),
        }
    }
    scope = "Только этот корпус, связанный с отелем; не перепись всех корпусов и не этажи номеров."
    levels = integer_levels(tags)
    if levels is None:
        reason = "building:levels в OSM нет или не целое число."
        return _fact(hotel_id, city, "building", bid, "H03", params, stamp, status="unknown",
                     unknown_reason="missing_aboveground_levels", unit="aboveground_storeys",
                     sources=[source], scope_note=scope,
                     confidence={"level": "unknown", "reason": reason},
                     recipe_attempts=_attempts("H03", {"H03-R1": NO_CADASTRE, "H03-R2": reason},
                                               None))  # fmt: skip
    try:
        skipped = int(float(str(tags.get("building:min_level") or 0).replace(",", ".")))
    except ValueError:
        skipped = 0
    value = {
        "aboveground_levels": max(0, levels - skipped),
        "raw_building_levels": levels,
        "skipped_levels_below_raised_part": skipped,
        "counting_convention": LEVELS_CONVENTION,
        "roof_levels": tags.get("roof:levels"),
        "underground_levels": tags.get("building:levels:underground"),
        "building_part": False,
        "not_room_floor_labels": True,
    }
    reason = (
        "Значение тега OSM у связанного с отелем корпуса, счёт этажей по правилам OSM; "
        "не осмотр. Время правки OSM — не время наблюдения."
    )
    return _fact(
        hotel_id, city, "building", bid, "H03", params, stamp, value=value, status="observed",
        unit="aboveground_storeys", recipe_id="H03-R2", sources=[source], scope_note=scope,
        confidence={"level": "medium", "reason": reason},
        recipe_attempts=_attempts("H03", {"H03-R1": NO_CADASTRE}, "H03-R2"),
    )  # fmt: skip


NO_CADASTRE = "Нет сопоставленной записи официального кадастра с правом использования и счётом."


# -------------------------------------------------------------------------------- сборка слоя


def load_raw(raw: Path, city: str) -> tuple[dict, dict[str, list[dict]]]:
    """Сырьё города: манифест (проверка sha256 каждого файла) и элементы по видам без повторов."""
    folder = raw / city
    path = folder / "manifest.json"
    if not path.is_file():
        raise PackError(f"{city}: нет {path} — сначала `fetch --raw {raw}`")
    manifest = json.loads(path.read_text("utf-8"))
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for f in manifest["files"]:
        file = folder / f["file"]
        if sha256_file(file) != f["sha256"]:
            raise PackError(f"{city}: sha256 {file} не совпадает с манифестом сырья")
        with gzip.open(file, "rb") as gz:
            body = json.loads(gz.read())
        if body.get("remark") and "error" in body["remark"]:
            raise PackError(f"{city}: {file} — неполный ответ Overpass: {body['remark'][:120]}")
        for el in body.get("elements", []):
            out[f["kind"]][osm_id(el)] = el
        del body
    kinds = Counter(f["kind"] for f in manifest["files"])
    if not kinds["buildings"] or not kinds["roads"] or not kinds["rail"]:
        raise PackError(f"{city}: в сырье нет зданий, дорог или путей: {dict(kinds)}")
    return manifest, {k: list(v.values()) for k, v in out.items()}


def raw_passport(manifest: dict, raw: Path, city: str) -> dict:
    """Паспорт сырья для фактов и манифеста. Срез PBF — `sha256` самого PBF (тот же
    `dataset_sha256`, что у фактов route-poi.1 пакета); Overpass — sha256 манифеста сырья."""
    files = manifest["files"]
    bases = sorted({f["osm_base"] for f in files if f.get("osm_base")})
    pbf = {f.get("source_sha256") for f in files}
    raw_sha = sha256_file(raw / city / "manifest.json")
    return {
        "provider": "OpenStreetMap/Geofabrik"
        if None not in pbf
        else "OpenStreetMap (Overpass API)",
        "osm_base": bases[-1] if bases else None,
        "osm_base_range": [bases[0], bases[-1]] if bases else None,
        "sha256": pbf.pop() if len(pbf) == 1 and None not in pbf else raw_sha,
        "raw_manifest_sha256": raw_sha,
        "fetched_at": max(f["fetched_at"] for f in files),
        "endpoints": sorted({f["url"] for f in files if f.get("url")}),
    }


def pack_rows(pack: Path, manifest: Manifest, kind: str) -> Iterator[dict]:
    """Строки вида `kind` из всех файлов пакета, кроме слоёв этой сборки (пересборка)."""
    for f in manifest.files:
        if f.kind == kind and f.path not in OWN_LAYERS:
            yield from iter_jsonl(pack / f.path)


def coverage_of(pack: Path, features: Iterable[str]) -> dict[str, dict]:
    m = read_manifest(pack)
    entry = next(f for f in m.files if f.kind == "coverage")
    rows = {r["feature_id"]: r for r in iter_jsonl(pack / entry.path)}
    keys = ("computed", "estimate", "unknown", "not_applicable")
    return {fid: {k: rows[fid].get(k, 0) for k in keys} for fid in features if fid in rows}


def build_city(raw: Path, city: str, packs: Path = PACKS) -> dict:
    """Связи, корпуса, фасады и факты одного города → четыре слоя пакета."""
    from shapely.geometry import shape

    from calculators.route_facades import enrich

    started = time.time()
    pack = packs / city
    manifest = read_manifest(pack)
    raw_manifest, elements = load_raw(raw, city)
    passport = raw_passport(raw_manifest, raw, city)
    stamp = passport["fetched_at"]
    before = coverage_of(pack, ("N02", "N03", "H03"))

    hotels = list(pack_rows(pack, manifest, "hotels"))
    by_id = {h["id"]: h for h in hotels}
    pack_buildings = {b["id"]: b for b in pack_rows(pack, manifest, "buildings")}
    links = list(pack_rows(pack, manifest, "hotel_buildings"))
    linked = {x["hotel_id"] for x in links}

    footprints = {}
    for el in elements.get("buildings", []):
        if (el.get("tags") or {}).get("building", "no") == "no":
            continue
        footprints[osm_id(el)] = (el, element_geometry(el))
    outcomes = link_by_point(hotels, footprints, skip=linked)
    # Проверка правила на прежних связях: что дала бы точка тем, кто уже связан по адресу.
    replay = Counter()
    for o in link_by_point(hotels, footprints, skip=set(by_id) - linked):
        old = {x["building_id"] for x in links if x["hotel_id"] == o.hotel_id}
        replay["same_building" if o.code == "linked" and set(o.candidates) <= old
               else o.code if o.code != "linked" else "other_building"] += 1  # fmt: skip

    new_links, new_buildings = [], {}
    for o in outcomes:
        if o.code != "linked":
            continue
        bid = o.candidates[0]
        new_links.append({"hotel_id": o.hotel_id, "building_id": bid, "method": METHOD,
                          "evidence": o.evidence})  # fmt: skip
        if bid not in pack_buildings and bid not in new_buildings:
            el, geom = footprints[bid]
            new_buildings[bid] = building_row(el, geom, city)
    building_rows = sorted(new_buildings.values(), key=lambda b: b["id"])
    facade_parts = {"buildings": building_rows}
    enrich(facade_parts, {"dataset_version": passport["osm_base"],
                          "dataset_sha256": passport["sha256"],
                          "snapshot_file": Path(BUILDINGS).name})  # fmt: skip
    facade_rows = sorted(facade_parts["facades"], key=lambda f: (f["building_id"], f["facade_id"]))

    # Факты, что уже есть в пакете: их id не повторяем, посчитанное не заменяем оценкой.
    fact_ids = set()
    have = defaultdict(set)  # (отель, характеристика) → субъекты фактов
    has_value = defaultdict(set)  # то же, но только факты со значением
    for row in pack_rows(pack, manifest, "facts"):
        fact_ids.add(row["fact_id"])
        if row["feature_id"] in ("N02", "N03", "H03"):
            have[(row["hotel_id"], row["feature_id"])].add(row["subject_id"])
            if row["status"] not in ("unknown", "not_applicable"):
                has_value[(row["hotel_id"], row["feature_id"])].add(row["subject_id"])

    # Оси — сразу в метрическую проекцию; сырые точки отпускаем (Москва: память).
    roads = Targets(city, [r for r in elements.pop("roads", [])
                           if (r.get("tags") or {}).get("highway") in BUSY],
                    raw_manifest["bbox"], passport)  # fmt: skip
    rail = Targets(city, [r for r in elements.pop("rail", []) if active_rail(r.get("tags") or {})],
                   raw_manifest["bbox"], passport)  # fmt: skip
    targets = {"N02": roads, "N03": rail}
    pack_source = next((s for s in manifest.sources if s.id == "d34_d39_osm"), None)

    def source_of(bid: str) -> tuple[dict, str]:
        if bid in new_buildings:
            el = footprints[bid][0]
            return {
                "provider": "OpenStreetMap", "url": f"https://www.openstreetmap.org/{bid}",
                "license": OSM_LICENSE, "attribution": "© OpenStreetMap contributors",
                "version": el.get("version"),
                "object_edited_at": osm_timestamp(el.get("timestamp")),
                "physical_observation_at": None, "dataset_version": passport["osm_base"],
                "dataset_sha256": passport["sha256"], "snapshot_file": Path(BUILDINGS).name,
            }, BUILDINGS  # fmt: skip
        return {
            "provider": "OpenStreetMap", "url": f"https://www.openstreetmap.org/{bid}",
            "license": OSM_LICENSE, "attribution": "© OpenStreetMap contributors",
            "physical_observation_at": None, "snapshot_file": "buildings.jsonl.gz",
            "dataset_version": pack_source.release if pack_source else None,
        }, "data/buildings.jsonl.gz"  # fmt: skip

    rows: list[dict] = []
    for link in [*links, *new_links]:
        hid, bid = link["hotel_id"], link["building_id"]
        building = new_buildings.get(bid) or pack_buildings.get(bid)
        if building is None or hid not in by_id:
            continue
        source, file = source_of(bid)
        if bid not in have[(hid, "H03")]:
            rows.append(levels_fact(hid, city, building, link, source, stamp))
        footprint = None
        for fid in ("N02", "N03"):
            if bid in have[(hid, fid)]:
                continue
            if footprint is None:
                footprint = _polygonal(shape(building["geometry"]))
            if footprint is not None:
                rows.append(building_distance_fact(hid, city, fid, building, footprint,
                                                   targets[fid], stamp, file))  # fmt: skip
    now_linked = linked | {x["hotel_id"] for x in new_links}
    for o in outcomes:
        if o.hotel_id in now_linked:
            continue
        for fid in ("N02", "N03"):
            if has_value[(o.hotel_id, fid)]:
                continue  # посчитанное или иное значение уже есть — оценкой не заменяем
            row = point_estimate_fact(by_id[o.hotel_id], city, fid, o, targets[fid], stamp)
            if row is not None:
                rows.append(row)

    rows.sort(key=lambda r: (r["hotel_id"], r["feature_id"], r["subject_id"], r["fact_id"]))
    ids = [r["fact_id"] for r in rows]
    clash = (set(ids) & fact_ids) | {i for i, n in Counter(ids).items() if n > 1}
    if clash:
        raise PackError(f"{city}: id фактов слоя уже заняты: {sorted(clash)[:5]}")
    link_rows = sorted(new_links, key=lambda x: (x["hotel_id"], x["building_id"]))

    source = {
        "provider": passport["provider"],
        "url": passport["endpoints"][0] if passport["endpoints"] else None,
        "release": passport["osm_base"],
        "sha256": passport["sha256"],
        "raw_manifest_sha256": passport["raw_manifest_sha256"],
        "license": OSM_LICENSE,
        "commercial_ok": True,
        "fetched_at": passport["fetched_at"],
    }
    layers = [(BUILDINGS, "buildings", building_rows, "buildings"),
              (FACADES, "facades", facade_rows, "facades"),
              (LINKS, "hotel_buildings", link_rows, "links"),
              (FACTS, "facts", rows, "facts")]  # fmt: skip
    for path, kind, data, name in layers:  # факты — последними: покрытие считается по всем
        write_jsonl_gz(pack / path, data)
        add_layer(pack, path, kind, len(data),
                  [PackSource(id=f"building_links_{name}:osm", method=VERSION,
                              **source)])  # fmt: skip
    after = coverage_of(pack, ("N02", "N03", "H03"))
    stats = {
        "city": city,
        "hotels": len(hotels),
        "linked_before": len(linked),
        "outcomes": dict(Counter(o.code for o in outcomes)),
        "replay_on_existing_links": dict(replay),
        "new_links": len(link_rows),
        "new_buildings": len(building_rows),
        "new_facades": len(facade_rows),
        "facts": dict(
            Counter(f"{r['feature_id']}:{r['subject_level']}:{r['status']}" for r in rows)
        ),  # fmt: skip
        "coverage_before": before,
        "coverage_after": after,
        "raw": passport,
        "seconds": round(time.time() - started, 1),
    }
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="step", required=True)
    for name in ("extract", "fetch", "build"):
        p = sub.add_parser(name)
        p.add_argument("--raw", required=True, type=Path,
                       help="папка сырья OSM вне репозитория (ODbL)")  # fmt: skip
        p.add_argument("--city", choices=CITIES, nargs="*", default=list(CITIES))
        p.add_argument("--packs", type=Path, default=PACKS)
    sub.choices["fetch"].add_argument(
        "--endpoint", action="append", help="зеркало Overpass (можно несколько, по порядку)"
    )
    sub.choices["extract"].add_argument(
        "--pbf", action="append", required=True, metavar="ГОРОД=ПУТЬ",
        help="срез Geofabrik города: moscow=…central-fed-district…, sochi=…southern-fed-district…",
    )  # fmt: skip
    args = ap.parse_args()
    raw = args.raw.resolve()
    if raw.is_relative_to(args.packs.resolve().parent):
        ap.error("сырьё OSM — вне репозитория: в пакет идут только результаты")
    pbfs = dict(x.split("=", 1) for x in getattr(args, "pbf", None) or [])
    for city in args.city:
        if args.step == "extract":
            if city not in pbfs:
                ap.error(f"нет --pbf {city}=<срез>")
            m = extract_pbf(raw, city, Path(pbfs[city]).resolve(), args.packs)
            print(f"{city}: " + ", ".join(f"{f['kind']} {f['elements']}" for f in m["files"])
                  + f", {m['seconds']} с")  # fmt: skip
        elif args.step == "fetch":
            m = fetch_city(raw, city, list(args.endpoint or ENDPOINTS), args.packs)
            print(f"{city}: файлов сырья {len(m['files'])}")
        else:
            stats = build_city(raw, city, args.packs)
            print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
