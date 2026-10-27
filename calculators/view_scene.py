"""Сцена высот вокруг отеля — справка к расчёту вида и солнца.

Задача 2б (`docs/tasks/lera.md`): вокруг отелей показа собрать соседние здания с этажностью и
высотой. Сам расчёт вида и солнца (`calculators/view_sun.py` лида) читает ту же выгрузку OSM и
сопоставление корпусов v9 напрямую — сцена его не кормит, это справка «что стоит вокруг отеля».

Что даёт: для каждого здания в радиусе — расстояние и азимут от точки отеля, высоту (из `height`,
иначе `building:levels` × 3 м), площадь контура и признак «своё здание». Высота не выдумывается:
если в OSM её нет, поле `height_m` = null, а `height_source` = `missing`.

«Своё здание» (`self_building`, отелю не препятствие) — корпус из сопоставления v9
(`results/facades/<город>_building_matches.jsonl`, статус `accepted_*`) или контур, внутри
которого лежит точка отеля; откуда отметка — в `self_source` (`v9_match` / `contains_point`).
Совпадение слов названия — только подсказка (`name_match`): «Отель Нептун» рядом с «Отелем
Фрегат» — сосед, а не корпус, поэтому по имени здание из препятствий не убирается.

Источник — OpenStreetMap (ODbL 1.0), только локальные данные: сети модуль не касается.
- Выгрузка v9 `cohort/osm_<город>_extended.jsonl` — та же, по которой считает лид. Лежит в
  материалах расчётов рядом с репозиторием (как у `pipeline/build_view_sun.py`) или `--extract`.
- Если выгрузки нет — закоммиченные ответы `data/scenes/cache/osm_map_*.json` (три отеля показа,
  собраны 26.10). Массовое чтение API редактирования OSM (api.openstreetmap.org) политика OSMF
  запрещает, поэтому сетевого пути здесь нет.

Запуск из корня репозитория:

    python -m calculators.view_scene --city sochi
    python -m calculators.view_scene --hotels 13736470 --radius 300 --extract <выгрузка>
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / "research_notes" / "Расчёт характеристик" / "cohort" / "hotels.jsonl"
NOTES = ROOT.parent / "research_notes" / "Расчёт характеристик"  # материалы вне репозитория
SCENE_VERSION = "view-scene-osm@2"
LEVEL_HEIGHT_M = 3.0
DEFAULT_RADIUS_M = 200.0
SHOW_HOTELS = {"sochi": ("13736470", "6443737", "8869683")}
# Слова, которые есть в названии почти любого отеля: по ним «то же здание» не узнать.
STOP_WORDS = frozenset(
    {
        "отель",
        "hotel",
        "гостиница",
        "гостевой",
        "курортный",
        "санаторий",
        "пансионат",
        "resort",
        "apart",
        "апарт",
        "сочи",
        "адлер",
        "сириус",
        "хоста",
        "дагомыс",
        "club",
        "клуб",
    }
)

Ring = list[tuple[float, float]]  # (lat, lon), замкнутый контур


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().lower().replace(",", ".").replace("м", "").replace("m", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def height_of(tags: dict[str, str]) -> tuple[float | None, str]:
    """Высота здания и откуда она: `height` → `levels` × 3 м → нет данных."""
    if (height := _float(tags.get("height"))) is not None and height > 0:
        return height, "height"
    if (levels := _float(tags.get("building:levels"))) is not None and levels > 0:
        return round(levels * LEVEL_HEIGHT_M, 1), "levels_x3m"
    return None, "missing"


def name_match(hotel_name: str | None, building_name: str | None) -> bool:
    """Подсказка, что здание — сам отель: общее отличительное слово названия (без «отель»,
    «Сочи» и подобных). Здание по ней из препятствий не убирается."""
    if not hotel_name or not building_name:
        return False

    def words(text: str) -> set[str]:
        return {w for w in re.split(r"\W+", text.lower()) if len(w) > 3 and w not in STOP_WORDS}

    return bool(words(hotel_name) & words(building_name))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Азимут от отеля на здание: 0 — север, 90 — восток."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return round((math.degrees(math.atan2(x, y)) + 360) % 360, 1)


def polygon_area_m2(points: Ring) -> float:
    """Площадь контура по формуле шнуровки в метрах (равнопромежуточная проекция вокруг центра)."""
    if len(points) < 3:
        return 0.0
    lat0 = sum(p[0] for p in points) / len(points)
    k = 111_320.0
    cos_lat = math.cos(math.radians(lat0))
    xy = [((lon - points[0][1]) * k * cos_lat, (lat - lat0) * k) for lat, lon in points]
    area = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area += x1 * y2 - x2 * y1
    return round(abs(area) / 2, 1)


def in_ring(lat: float, lon: float, ring: Ring) -> bool:
    """Точка внутри контура (луч на восток, чётность пересечений)."""
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (lat1 > lat) != (lat2 > lat):
            cross = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < cross:
                inside = not inside
    return inside


def _open(ring: Ring) -> Ring:
    return ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring


def contains_point(polygons: list[dict[str, Any]], lat: float, lon: float) -> bool:
    return any(
        in_ring(lat, lon, p["outer"]) and not any(in_ring(lat, lon, h) for h in p["inner"])
        for p in polygons
    )


def cache_elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Здания из закоммиченного ответа `osm_map_*.json`: один внешний контур на way."""
    elements = []
    for element in payload.get("elements", []):
        ring = [(p["lat"], p["lon"]) for p in element.get("geometry") or []]
        if len(ring) >= 3:
            elements.append(
                {
                    "osm_id": f"{element.get('type', 'way')}/{element['id']}",
                    "tags": element.get("tags") or {},
                    "polygons": [{"outer": ring, "inner": []}],
                }
            )
    return elements


def extract_elements(path: Path, boxes: list[tuple[float, float, float, float]]) -> list[dict]:
    """Здания (ways и relations) из выгрузки v9, попавшие хотя бы в одну рамку (s, w, n, e)."""
    import shapely

    elements = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            tags = row.get("tags") or {}
            if tags.get("building") in (None, "no") or row.get("osm_type") == "node":
                continue
            geom = shapely.from_wkb(bytes.fromhex(row["geometry_wkb"]))
            if geom.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            west, south, east, north = geom.bounds
            if not any(
                s <= north and w <= east and n >= south and e >= west for s, w, n, e in boxes
            ):
                continue
            parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
            polygons = [
                {
                    "outer": [(y, x) for x, y in part.exterior.coords],
                    "inner": [[(y, x) for x, y in ring.coords] for ring in part.interiors],
                }
                for part in parts
            ]
            elements.append(
                {
                    "osm_id": f"{row['osm_type']}/{row['osm_id']}",
                    "tags": tags,
                    "polygons": polygons,
                }
            )
    return elements


def build_scene(
    hotel: dict[str, Any],
    elements: list[dict[str, Any]],
    radius_m: float,
    source: dict[str, Any],
    own: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Здания в радиусе от точки отеля. `own` — корпуса отеля из сопоставления v9."""
    lat, lon = float(hotel["latitude"]), float(hotel["longitude"])
    obstacles = []
    for element in elements:
        tags = element.get("tags") or {}
        # Центр — среднее вершин без повтора замыкающей: иначе он зависит от того, с какой
        # вершины начат контур (у выгрузки и ответа API — с разных).
        outer = [p for polygon in element["polygons"] for p in _open(polygon["outer"])]
        if not outer:
            continue
        clat = sum(p[0] for p in outer) / len(outer)
        clon = sum(p[1] for p in outer) / len(outer)
        distance = haversine_m(lat, lon, clat, clon)
        if distance > radius_m:
            continue
        if element["osm_id"] in own:
            self_source = "v9_match"
        elif contains_point(element["polygons"], lat, lon):
            self_source = "contains_point"
        else:
            self_source = None
        height, source_kind = height_of(tags)
        area = sum(
            polygon_area_m2(p["outer"]) - sum(polygon_area_m2(h) for h in p["inner"])
            for p in element["polygons"]
        )
        obstacles.append(
            {
                "osm_id": element["osm_id"],
                "name": tags.get("name"),
                "self_building": self_source is not None,
                "self_source": self_source,
                "name_match": name_match(hotel.get("name"), tags.get("name")),
                "building": tags.get("building"),
                "distance_m": round(distance, 1),
                "azimuth_deg": bearing_deg(lat, lon, clat, clon),
                "height_m": height,
                "height_source": source_kind,
                "levels": _float(tags.get("building:levels")),
                "footprint_m2": round(area, 1),
            }
        )
    obstacles.sort(key=lambda o: (o["distance_m"], o["osm_id"]))
    others = [o for o in obstacles if not o["self_building"]]
    return {
        "scene_version": SCENE_VERSION,
        "hotel_id": str(hotel["hotel_id"]),
        "hotel_name": hotel.get("name"),
        "latitude": lat,
        "longitude": lon,
        "radius_m": radius_m,
        "source": source,
        "counts": {
            "buildings": len(obstacles),
            "self_buildings": len(obstacles) - len(others),
            "with_height": sum(1 for o in obstacles if o["height_m"] is not None),
            "with_levels": sum(1 for o in obstacles if o["levels"] is not None),
            "nearest_m": others[0]["distance_m"] if others else None,  # ближайшее чужое
        },
        "obstacles": obstacles,
    }


def load_hotels(city: str, wanted: tuple[str, ...]) -> list[dict[str, Any]]:
    hotels = []
    with COHORT.open("r", encoding="utf-8") as f:
        for line in f:
            hotel = json.loads(line)
            if hotel.get("city_id") != city or str(hotel["hotel_id"]) not in wanted:
                continue
            if hotel.get("latitude") is None or hotel.get("longitude") is None:
                continue
            hotels.append(hotel)
    return hotels


def load_own(path: Path | None) -> dict[str, frozenset[str]]:
    """Корпуса отелей из сопоставления v9: hotel_id → здания OSM (только принятые)."""
    own: dict[str, set[str]] = {}
    if path is None or not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if str(row.get("match_status", "")).startswith("accepted"):
                own.setdefault(str(row["hotel_id"]), set()).add(row["osm_id"])
    return {k: frozenset(v) for k, v in own.items()}


def box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Рамка (s, w, n, e) с запасом на размер здания: центр контура в радиусе, края — дальше."""
    margin = radius_m + 300
    dlat = margin / 111_000
    dlon = margin / (111_000 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def extract_source(path: Path) -> dict[str, Any]:
    manifest = path.with_name(path.name.replace(".jsonl", "_manifest.json"))
    meta = json.loads(manifest.read_text("utf-8")) if manifest.is_file() else {}
    return {
        "provider": "OpenStreetMap",
        "license": "ODbL 1.0",
        "dataset": f"research_notes/Расчёт характеристик/cohort/{path.name} (сцена v9)",
        "release": meta.get("source_date"),
        "sha256": meta.get("sha256"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="sochi")
    parser.add_argument("--hotels", nargs="+", help="ID отелей; по умолчанию отели показа")
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    parser.add_argument("--extract", type=Path, help="выгрузка OSM v9 (JSONL с geometry_wkb)")
    parser.add_argument("--matches", type=Path, help="сопоставление корпусов v9 (JSONL)")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "scenes" / "cache")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "scenes")
    args = parser.parse_args()

    wanted = tuple(args.hotels) if args.hotels else SHOW_HOTELS.get(args.city)
    if not wanted:
        print(f"для {args.city} нет отелей показа — укажите --hotels")
        return 1
    hotels = load_hotels(args.city, wanted)
    if not hotels:
        print(f"нет отелей {wanted} в когорте")
        return 1
    extract = args.extract or NOTES / "cohort" / f"osm_{args.city}_extended.jsonl"
    matches = args.matches or NOTES / "results" / "facades" / f"{args.city}_building_matches.jsonl"
    own = load_own(matches)
    print(f"корпуса v9: {matches if own else 'нет — своё здание только по точке в контуре'}")
    self_rule = "точка отеля внутри контура"
    if own:
        self_rule = f"сопоставление корпусов v9 ({matches.name}) или {self_rule}"
    elements: list[dict[str, Any]] | None = None
    if extract.is_file():
        boxes = [box(float(h["latitude"]), float(h["longitude"]), args.radius) for h in hotels]
        elements = extract_elements(extract, boxes)
        print(f"выгрузка: {extract} — зданий у отелей {len(elements)}")
    args.out.mkdir(parents=True, exist_ok=True)

    for hotel in hotels:
        lat, lon = float(hotel["latitude"]), float(hotel["longitude"])
        if elements is not None:
            hotel_elements, source = elements, extract_source(extract)
        else:
            cache_path = args.cache / f"osm_map_{lat:.5f}_{lon:.5f}_{int(args.radius)}.json"
            if not cache_path.is_file():
                print(f"{hotel['hotel_id']}: нет ни выгрузки, ни кеша — пропуск")
                continue
            payload = json.loads(cache_path.read_text("utf-8"))
            hotel_elements = cache_elements(payload)
            source = {
                "provider": "OpenStreetMap",
                "license": "ODbL 1.0",
                "query": payload.get("_query"),
                "fetched_at": payload.get("_fetched_at"),
            }
        source = source | {"self_buildings": self_rule}
        own_ids = own.get(str(hotel["hotel_id"]), frozenset())
        scene = build_scene(hotel, hotel_elements, args.radius, source, own_ids)
        path = args.out / f"scene_{args.city}_{hotel['hotel_id']}.json"
        path.write_text(json.dumps(scene, ensure_ascii=False, indent=1) + "\n", "utf-8")
        counts = scene["counts"]
        print(
            f"{hotel['hotel_id']} {str(hotel.get('name'))[:34]:36} "
            f"зданий {counts['buildings']:3} (своих {counts['self_buildings']}), "
            f"с высотой {counts['with_height']:3}, ближайшее чужое {counts['nearest_m']} м "
            f"→ {path.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
