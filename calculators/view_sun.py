"""V05 «закат из окна» и V01 «море из окна» по окнам «здание:фасад:floorN» (D17, D39, D40).

Интервальный расчёт лучей (D40). Каждая неточная величина — интервал: высота препятствия,
земля под ним и под окном, высота глаза на этаже. Ответ «да» или «нет» — только если он один
при любых значениях из интервалов, в любом месте фасада (точки через SAMPLE_STEP_M) и, для
заката, при любом прочтении «виден в последние 30 минут»: «да» — диск виден все эти минуты,
«нет» — ни минуты. Иначе «неизвестно» с причиной и зданием, которое решает исход (очередь на
проверку этажности по картам, D33), а рядом — оценка по центральным значениям (D18).

Входы — только для сборки пакета (группа builder), у жюри расчёт не запускается:
- контуры зданий и этажность — сцена OSM v9 (`cohort/osm_sochi_extended.jsonl`), корпуса
  отелей — сопоставление v9 (`results/facades/sochi_building_matches.jsonl`), ID фасадов —
  как в веере v9 (`facade_segments` в `scripts/facades.py`);
- этажность, проверенная командой по картам (`data/curated/*_building_heights.csv`, D33);
- 3D-GloBFP v2 — высоты по спутниковым снимкам: только для зданий без высоты и этажности
  в OSM и ±16 м (90-й процентиль ошибки против тега `height` OSM в Сочи, n = 39);
- Copernicus DEM GLO-30 — поверхность (рельеф, кроны, крыши), сетка 1″; для зданий до 300 м²
  без данных — потолок высоты (максимум поверхности + 10 м: на 38 тыс. зданиях Сочи с известной
  этажностью ниже настоящей высоты в 0,1–0,3% случаев);
- ESA WorldCover 2021 — деревья (класс 10) и вода (класс 80);
- положение Солнца — NREL SPA (pvlib).
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id

VERSION = "view-sun-intervals@2"

# Высоты, м. Этаж и глаз — типовой диапазон; верх здания по этажности — по 240 зданиям Сочи,
# у которых в OSM есть и `height`, и `building:levels` (у 3–9 этажей: 3,0–5,3 м на этаж с крышей).
FLOOR_M = (2.9, 4.0)
EYE_M = (1.2, 1.8)
LEVEL_TOP = (2.9, 4.0, 4.0)  # [2,9·L; 4,0·L + 4]
TAG_TOP = (0.9, 1.1, 1.0)  # тег height: [0,9·h; 1,1·h + 1]
GLOBFP_ERR_M = 16.2
UNKNOWN_TOP_M = (2.5, 100.0)  # здание без высоты, этажности и GloBFP
DSM_CAP_AREA_M2 = 300.0
DSM_CAP_MARGIN_M = 10.0
TREE_TOP_M = 25.0
DSM_SIGMA_M = 2.0  # GLO-30: относительная точность по высоте на ровном месте
LOW_EYE_M = 6.0  # ниже — уличные деревья и ограды, которых нет на картах

# Оценка (D18, вне обязательных условий): центральные значения вместо интервалов.
FLOOR_MID_M, EYE_MID_M = 3.3, 1.5
LEVEL_MID = (3.3, 1.0)  # верх по этажности: 3,3·L + 1 (медиана по Сочи — 3,5 м на этаж с крышей)
UNMEASURED_MID_M = 7.6  # здание без данных — не ниже двух этажей
TREE_MID_M = 12.0

NEAR_M = 300.0  # ближняя зона: деревья и земля — от точки окна
BUILDINGS_M = 4000.0  # контуры зданий: GLO-30 высокие дома занижает, дальше — только поверхность
FAR_MAX_M = 250_000.0  # дальняя зона: поверхность GLO-30 — от корпуса
FAR_LATERAL_M = 60.0  # дальняя зона: параллельные лучи через корпус не реже (плюс 3×3 пикселя)
REGION_MAX_M = 5642.0  # Эльбрус: выше рельефа в регионе нет
SEA_RUN_M = 500.0  # «открытый участок моря»: вода на уровне моря на луче не короче
SEA_LEVEL_M = 2.0  # вода выше этого по GLO-30 — река или озеро, не море
SEA_MAX_M = 12_000.0
SEA_STEP_M = 15.0

FRONT_YES_DEG = 75.0  # солнце не дальше от нормали фасада — окно его видит
FRONT_NO_DEG = 90.0  # за плоскостью фасада
LAST_MINUTES = 30
STEP_MINUTES = 2
SUNSET_ELEVATION = -0.833  # центр диска, геометрическая высота
UPPER_LIMB_DEG = 0.266
REFRACTION_K = 0.13
EARTH_R = 6_371_000.0

MIN_FACADE_M = 3.0  # короче — окна номера на таком отрезке не ждём
SAMPLE_STEP_M = 2.0  # окно — любое место фасада: точки не реже, от 0,3 м от углов
SECTOR_DEG = 45.0  # обзор из окна для V01: ±45° от нормали
SECTOR_STEP_DEG = 1.0
WATER, TREES = 80, 10

V05_PARAMS = {"last_minutes": LAST_MINUTES}
V01_PARAMS = {"sector_deg": int(2 * SECTOR_DEG), "max_distance_m": int(SEA_MAX_M)}

URLS = {
    "osm": "https://www.openstreetmap.org/",
    "globfp": "https://doi.org/10.5281/zenodo.15487037",
    "copdem": "https://copernicus-dem-30m.s3.amazonaws.com/",
    "worldcover": "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/",
}

# Что решает исход «неизвестно» → код причины (core/reasons.py).
DECIDER_REASONS = {
    "osm_levels": "view_obstacle_height",
    "curated": "view_obstacle_height",
    "osm_height": "view_obstacle_height_tag",
    "construction": "view_obstacle_height",
    "globfp": "view_obstacle_satellite",
    "globfp_only": "view_obstacle_satellite",
    "dsm_cap": "view_obstacle_unmeasured",
    "unmeasured": "view_obstacle_unmeasured",
    "trees": "view_trees",
    "landcover_missing": "view_landcover_missing",
    "ground": "view_terrain",
    "terrain": "view_terrain",
    "beyond": "view_terrain_beyond",
}
BUILDING_KINDS = frozenset(
    {"osm_levels", "osm_height", "curated", "construction", "globfp", "dsm_cap", "unmeasured"}
)
# Ответ не зависит от этажа — один факт на весь фасад (D39); иначе — по этажам, которые есть.
FLOOR_FREE = frozenset({"sun_behind_facade", "sun_behind_facade_all_year", "no_sea"})


# --- Данные ---


def _num(s: Any) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:m|м)?\s*", str(s)) if s is not None else None
    return float(m.group(1).replace(",", ".")) if m else None


def integer_levels(tags: dict[str, Any]) -> int | None:
    """Как в v9: целое `building:levels` ≤ 180."""
    n = _num(tags.get("building:levels"))
    return int(n) if n is not None and n.is_integer() and 0 < n <= 180 else None


def top_interval(
    tags: dict[str, Any], globfp_m: float | None, curated: dict[str, Any] | None = None
) -> tuple[float, float, str]:
    """Высота верха здания над землёй: интервал и откуда он."""
    if curated:
        if curated.get("height_m"):
            h = float(curated["height_m"])
            return TAG_TOP[0] * h, TAG_TOP[1] * h + TAG_TOP[2], "curated"
        levels = int(curated["levels"])
        return LEVEL_TOP[0] * levels, LEVEL_TOP[1] * levels + LEVEL_TOP[2], "curated"
    if tags.get("building") == "construction":
        # Стройка: может быть ещё ниже, может быть выше проекта (краны) — «нет» по ней не говорим.
        h = _num(tags.get("height"))
        levels = integer_levels(tags)
        hi = (
            h * TAG_TOP[1] + TAG_TOP[2]
            if h
            else (LEVEL_TOP[1] * levels + LEVEL_TOP[2] if levels else UNKNOWN_TOP_M[1])
        )
        return -math.inf, hi, "construction"
    h = _num(tags.get("height"))
    if h:
        return TAG_TOP[0] * h, TAG_TOP[1] * h + TAG_TOP[2], "osm_height"
    levels = integer_levels(tags)
    if levels:
        return LEVEL_TOP[0] * levels, LEVEL_TOP[1] * levels + LEVEL_TOP[2], "osm_levels"
    if globfp_m is not None:
        return max(UNKNOWN_TOP_M[0], globfp_m - GLOBFP_ERR_M), globfp_m + GLOBFP_ERR_M, "globfp"
    return (*UNKNOWN_TOP_M, "unmeasured")


def top_central(
    tags: dict[str, Any], globfp_m: float | None, curated: dict[str, Any] | None = None
) -> float:
    """Высота верха для оценки; NaN — посчитать по поверхности GLO-30 (`ensure_ground`)."""
    src = curated or {}
    h = _num(src.get("height_m")) if curated else _num(tags.get("height"))
    if h:
        return h
    levels = int(src["levels"]) if curated else integer_levels(tags)
    if levels:
        return LEVEL_MID[0] * levels + LEVEL_MID[1]
    return globfp_m if globfp_m is not None else math.nan


def floors_of(tags: dict[str, Any], curated: dict[str, Any] | None = None) -> int | None:
    if curated and curated.get("levels"):
        return int(curated["levels"])
    return integer_levels(tags)


def eye_interval(floor: int) -> tuple[float, float]:
    """Глаз над землёй у фасада на этаже `floor` (первый — на уровне земли)."""
    return (floor - 1) * FLOOR_M[0] + EYE_M[0], (floor - 1) * FLOOR_M[1] + EYE_M[1]


def eye_central(floor: int) -> float:
    return (floor - 1) * FLOOR_MID_M + EYE_MID_M


def load_curated(path: Path | None) -> dict[str, dict[str, Any]]:
    """Этажность, проверенная человеком по картам или на месте (D33): building_id → строка."""
    if path is None or not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("levels") or r.get("height_m"))]
    for r in rows:
        if not (r.get("checked_by") and r.get("checked_at") and r.get("source_url")):
            raise ValueError(f"{path.name}: {r['building_id']} — без проверяющего, даты или ссылки")
    return {r["building_id"]: r for r in rows}


def facade_segments(geom) -> list[dict[str, Any]]:
    """Отрезки контура с внешней нормалью — те же ID, что в веере v9 (`p{i}:r{j}:e{k}`)."""
    from shapely.geometry.polygon import orient

    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    sides = []
    for pi, p in enumerate(polys):
        p = orient(p, sign=1)
        for ri, ring in enumerate([p.exterior, *p.interiors]):
            coords = list(ring.coords)
            for ei, (a, b) in enumerate(zip(coords, coords[1:], strict=False)):
                dx, dy = b[0] - a[0], b[1] - a[1]
                length = math.hypot(dx, dy)
                if length < 0.01:
                    continue
                nx, ny = dy / length, -dx / length  # здание слева от обхода
                sides.append(
                    {
                        "facade_id": f"p{pi}:r{ri}:e{ei}",
                        "a": a,
                        "b": b,
                        "normal": (nx, ny),
                        "azimuth": math.degrees(math.atan2(nx, ny)) % 360,
                        "length": length,
                        "courtyard": ri > 0,
                    }
                )
    return sides


class Raster:
    """Растры в памяти: значение, минимумы 3×3 и 5×5, максимум 3×3 вокруг пикселя."""

    def __init__(self, paths: Iterable[Path], bbox: tuple[float, ...] | None = None) -> None:
        import rasterio
        from rasterio.windows import Window, from_bounds
        from scipy.ndimage import maximum_filter, minimum_filter

        self.tiles = []
        self.bounds = []  # что прочитано: (запад, юг, восток, север)
        for path in paths:
            with rasterio.open(path) as ds:
                if bbox:
                    win = from_bounds(*bbox, ds.transform)  # наружу до целых пикселей
                    win = win.round_offsets(op="floor").round_lengths(op="ceil")
                    win = win.intersection(Window(0, 0, ds.width, ds.height))
                    a = ds.read(1, window=win)
                    t = ds.window_transform(win)
                else:
                    a, t = ds.read(1), ds.transform
            surface = a.dtype.kind == "f"
            nodata = a == -32767 if surface else a == 0  # у WorldCover 0 — нет данных
            mn = minimum_filter(a, 3) if surface else None
            mx = maximum_filter(a, 3) if surface else None
            mn5 = minimum_filter(a, 5) if surface else None
            self.tiles.append((a, mn, mx, mn5, ~t, nodata))
            west, north = t * (0, 0)
            east, south = t * (a.shape[1], a.shape[0])
            self.bounds.append((west, south, east, north))

    def sample(self, lon, lat, ground: bool = False):
        """(значение, минимум, максимум 3×3, покрыто) для массивов точек.

        Минимум — 3×3, а при `ground=True` — 5×5 (~150 м): оценка земли сверху. Вне прочитанного —
        NaN и «не покрыто»: такую точку нельзя считать ни водой, ни сушей без деревьев."""
        import numpy as np

        n = len(lon)
        val = np.full(n, np.nan)
        mn = np.full(n, np.nan)
        mx = np.full(n, np.nan)
        for a, amn, amx, amn5, inv, nodata in self.tiles:
            cols, rows = inv * (np.asarray(lon), np.asarray(lat))
            c, r = np.floor(cols).astype(int), np.floor(rows).astype(int)
            ok = (r >= 0) & (r < a.shape[0]) & (c >= 0) & (c < a.shape[1]) & np.isnan(val)
            ok[ok] &= ~nodata[r[ok], c[ok]]
            val[ok] = a[r[ok], c[ok]]
            if amn is not None:
                mn[ok] = (amn5 if ground else amn)[r[ok], c[ok]]
                mx[ok] = amx[r[ok], c[ok]]
        return val, mn, mx, ~np.isnan(val)


@dataclass
class Obstacles:
    """Препятствия на одном луче: расстояние от окна и интервал абсолютной высоты верха."""

    d: Any
    lo: Any
    hi: Any
    mid: Any  # центральное значение — для оценки (D18); NaN — оценки нет
    kind: list[str]
    ids: list[str]


@dataclass
class Scene:
    project: Any  # lon/lat → м (азимутальная проекция города)
    inverse: Any  # м → lon/lat
    geod: Any
    polys: Any  # массив контуров препятствий в метрах: здания OSM и GloBFP, деревья OSM
    tree: Any
    top_lo: Any
    top_mid: Any
    top_hi: Any
    kind: list[str]
    ids: list[str]
    osm: dict[str, dict[str, Any]]  # здание OSM → теги, контур в метрах, индекс в polys
    curated: dict[str, dict[str, Any]]
    dsm: Raster
    worldcover: Raster
    land_cells: set[tuple[int, int]]  # клетки 1°×1° с сушей (список листов Copernicus DEM)
    ground_lo: Any = None  # земля под препятствием, м над уровнем моря: считается по требованию
    ground_mid: Any = None
    ground_hi: Any = None
    cap: Any = None  # потолок верха по GLO-30 для небольших зданий без данных (абсолютный)
    far_cache: dict[tuple, Any] = field(default_factory=dict)


def load_scene(
    scene_path: Path,
    globfp_path: Path,
    dsm_paths: list[Path],
    worldcover_path: Path,
    tile_list: Path,
    center: tuple[float, float],
    bbox: tuple[float, float, float, float],
    curated_path: Path | None = None,
) -> Scene:
    import numpy as np
    import shapely
    from pyproj import Geod, Transformer

    lon0, lat0 = center
    proj = f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m"
    fwd = Transformer.from_crs("EPSG:4326", proj, always_xy=True)
    inv = Transformer.from_crs(proj, "EPSG:4326", always_xy=True)

    def metric(g):
        return shapely.transform(g, lambda xy: np.column_stack(fwd.transform(xy[:, 0], xy[:, 1])))

    osm: dict[str, dict[str, Any]] = {}
    trees = []
    with scene_path.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            t = r.get("tags") or {}
            building = any(t.get(k) not in (None, "no") for k in ("building", "building:part"))
            treeish = t.get("natural") in ("tree", "tree_row", "wood")
            treeish = treeish or t.get("landuse") == "forest"
            if not building and not treeish:
                continue
            g = shapely.from_wkb(bytes.fromhex(r["geometry_wkb"]))
            if g.is_empty:
                continue
            gm = metric(g)
            sid = f"{r['osm_type']}/{r['osm_id']}"
            if building and g.geom_type in ("Polygon", "MultiPolygon"):
                osm[sid] = {
                    "tags": t,
                    "geom": gm if gm.is_valid else gm.buffer(0),
                    "version": r.get("version"),
                    "edited_at": r.get("timestamp"),
                }
            elif not building:
                area = gm.geom_type in ("Polygon", "MultiPolygon")
                trees.append((sid, gm if area else gm.buffer(4.0)))

    gb_geoms, gb_h = [], []
    with globfp_path.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            gb_geoms.append(metric(shapely.from_wkb(bytes.fromhex(r["geometry_wkb"]))))
            gb_h.append(float(r["attrs"]["Height"]))
    gb_arr = np.array(gb_geoms, dtype=object)
    ids = list(osm)
    om = np.array([osm[i]["geom"] for i in ids], dtype=object)
    pairs = shapely.STRtree(gb_arr).query(om, predicate="intersects")
    inter = shapely.area(shapely.intersection(om[pairs[0]], gb_arr[pairs[1]]))
    best: dict[int, tuple[float, int]] = {}
    for i, j, a in zip(pairs[0], pairs[1], inter, strict=True):
        if a > best.get(i, (0.0, -1))[0]:
            best[i] = (a, j)
    used = np.zeros(len(gb_geoms), bool)  # контур GloBFP отдал высоту зданию OSM
    for i, sid in enumerate(ids):
        a, j = best.get(i, (0.0, -1))
        ok = j >= 0 and (a >= 0.5 * om[i].area or a >= 0.5 * gb_geoms[j].area)
        osm[sid]["globfp"] = gb_h[j] if ok else None
        used[j] |= ok

    land = set()
    for name in tile_list.read_text("utf-8").split():
        m = re.search(r"_([NS])(\d\d)_00_([EW])(\d\d\d)_00_", name)
        if m:
            la = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
            lo = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
            land.add((la, lo))

    curated = load_curated(curated_path)
    polys, top_lo, top_mid, top_hi, kind, pids = [], [], [], [], [], []
    for sid in ids:
        tags, gb, cur = osm[sid]["tags"], osm[sid]["globfp"], curated.get(sid)
        lo, hi, k = top_interval(tags, gb, cur)
        osm[sid]["index"] = len(polys)
        polys.append(osm[sid]["geom"])
        top_lo.append(lo)
        top_mid.append(top_central(tags, gb, cur))
        top_hi.append(hi)
        kind.append(k)
        pids.append(sid)
    # Контуры GloBFP, не отдавшие высоту зданию OSM: здания, которых нет в OSM, или сдвинутые
    # копии. Может быть, а может и нет — «нет» по ним не говорим, «да» они могут закрыть.
    for j in np.flatnonzero(~used):
        polys.append(gb_geoms[j])
        top_lo.append(-math.inf)
        top_mid.append(gb_h[j])
        top_hi.append(gb_h[j] + GLOBFP_ERR_M)
        kind.append("globfp_only")
        pids.append(f"globfp/{j}")
    for sid, g in trees:
        polys.append(g)
        top_lo.append(-math.inf)
        top_mid.append(TREE_MID_M)
        top_hi.append(TREE_TOP_M)
        kind.append("trees")
        pids.append(sid)

    arr = np.array(polys, dtype=object)
    n = len(arr)
    return Scene(
        project=fwd,
        inverse=inv,
        geod=Geod(ellps="WGS84"),
        polys=arr,
        tree=shapely.STRtree(arr),
        top_lo=np.array(top_lo),
        top_mid=np.array(top_mid),
        top_hi=np.array(top_hi),
        kind=kind,
        ids=pids,
        osm=osm,
        curated=curated,
        dsm=Raster(dsm_paths),
        worldcover=Raster([worldcover_path], bbox=bbox),
        land_cells=land,
        ground_lo=np.full(n, np.nan),
        ground_mid=np.full(n, np.nan),
        ground_hi=np.full(n, np.nan),
        cap=np.full(n, np.nan),
    )


def _footprint_samples(scene: Scene, geom, buffer_m: float):
    import numpy as np
    import shapely

    ring = shapely.boundary(geom.buffer(buffer_m) if buffer_m else geom)
    xy = shapely.get_coordinates(shapely.segmentize(ring, 15.0))
    if not buffer_m:
        c = geom.centroid
        xy = np.vstack([xy, [[c.x, c.y]]])
    return scene.inverse.transform(xy[:, 0], xy[:, 1])


def ensure_ground(scene: Scene, idx) -> None:
    """Земля и потолок по GLO-30 для препятствий `idx` — один раз на препятствие.

    Земля — по кольцу 15 м вокруг контура. GLO-30 — поверхность (крыши, кроны), она не ниже
    земли. Нижняя граница — минимум 3×3 пикселей (~90 м) по кольцу: у берега это пляж и море,
    то есть заведомо не выше земли. Центр — 10-й процентиль кольца (улица, двор), верхняя
    граница — он же плюс DSM_SIGMA_M: в плотной застройке и под кронами он выше земли, и это
    только расширяет интервал.
    """
    import numpy as np

    for i in idx:
        if not math.isnan(scene.ground_lo[i]):
            continue
        lon, lat = _footprint_samples(scene, scene.polys[i], 15.0)
        val, mn3, _, ok = scene.dsm.sample(lon, lat)
        if not ok.any():
            scene.ground_lo[i] = scene.ground_mid[i] = scene.ground_hi[i] = -math.inf
            continue  # нет рельефа: см. near_obstacles
        mid = float(np.percentile(val[ok], 10))
        scene.ground_lo[i] = min(float(np.min(mn3[ok])), mid) - DSM_SIGMA_M
        scene.ground_mid[i] = mid
        scene.ground_hi[i] = mid + DSM_SIGMA_M
        if scene.kind[i] not in ("unmeasured", "globfp_only"):
            continue
        lon, lat = _footprint_samples(scene, scene.polys[i], 0.0)
        roof, _, mx, ok = scene.dsm.sample(lon, lat)
        if scene.kind[i] == "unmeasured":
            top = float(np.max(roof[ok])) - mid if ok.any() else 0.0
            scene.top_mid[i] = max(top, UNMEASURED_MID_M)
        if ok.any() and scene.polys[i].area < DSM_CAP_AREA_M2:
            scene.cap[i] = float(np.max(mx[ok])) + DSM_CAP_MARGIN_M
            if scene.kind[i] == "unmeasured":
                scene.kind[i] = "dsm_cap"


# --- Лучи ---


def _rise(d, alt_deg):
    """Насколько луч к Солнцу выше точки на земле на расстоянии d: наклон плюс кривизна Земли."""
    import numpy as np

    return d * np.tan(np.radians(alt_deg)) + d * d * (1 - REFRACTION_K) / (2 * EARTH_R)


def _drop(d):
    return d * d * (1 - REFRACTION_K) / (2 * EARTH_R)


def _unit(az: float) -> tuple[float, float]:
    return math.sin(math.radians(az)), math.cos(math.radians(az))


def near_obstacles(
    scene: Scene, x: float, y: float, az: float, g_w: tuple, reach: float
) -> Obstacles:
    """Контуры до `reach` и деревья, земля WorldCover/GLO-30 до NEAR_M — от точки окна.

    Нет WorldCover — там могут быть деревья: верх допускает их высоту (`landcover_missing`)."""
    import numpy as np
    import shapely

    ux, uy = _unit(az)
    p = shapely.Point(x, y)
    line = shapely.LineString([(x, y), (x + ux * reach, y + uy * reach)])
    idx = scene.tree.query(line, predicate="intersects")
    ensure_ground(scene, idx)
    dist = shapely.distance(p, shapely.intersection(line, scene.polys[idx]))
    g_lo, g_mid, g_hi = scene.ground_lo[idx], scene.ground_mid[idx], scene.ground_hi[idx]
    bad = ~np.isfinite(g_lo)
    g_lo[bad], g_mid[bad], g_hi[bad] = g_w
    lo = g_lo + scene.top_lo[idx]
    hi = np.fmin(g_hi + scene.top_hi[idx], scene.cap[idx])
    mid = g_mid + np.nan_to_num(scene.top_mid[idx], nan=UNMEASURED_MID_M)
    kind = [scene.kind[i] for i in idx]
    ids = [scene.ids[i] for i in idx]

    d = np.arange(5.0, min(NEAR_M, reach) + 1, 5.0)
    lon, lat = scene.inverse.transform(x + ux * d, y + uy * d)
    cls, _, _, covered = scene.worldcover.sample(lon, lat)
    _, mn5, _, ok = scene.dsm.sample(lon, lat, ground=True)
    tree = (cls == TREES) | ~covered
    near_hi = np.where(ok, mn5 + DSM_SIGMA_M, np.inf) + np.where(tree, TREE_TOP_M, 0.0)
    near_mid = np.where(ok, mn5, np.nan) + np.where(cls == TREES, TREE_MID_M, 0.0)
    near_kind = [
        "landcover_missing" if not c else "trees" if t else "ground"
        for c, t in zip(covered, cls == TREES, strict=True)
    ]
    return Obstacles(
        np.concatenate([dist, d]),
        np.concatenate([lo, np.full(len(d), -np.inf)]),
        np.concatenate([hi, near_hi]),
        np.concatenate([mid, near_mid]),
        kind + near_kind,
        ids + ["worldcover" if k != "ground" else "copdem" for k in near_kind],
    )


def _half_width(geom, az: float) -> float:
    """Насколько корпус выходит в стороны от луча через его центр (поперёк направления)."""
    import shapely

    ux, uy = _unit(az)
    c = geom.centroid
    xy = shapely.get_coordinates(geom)
    return float(max(abs((x - c.x) * uy - (y - c.y) * ux) for x, y in xy))


def far_profile(scene: Scene, bid: str, az: float, max_d: float):
    """Поверхность GLO-30 от NEAR_M до max_d — по параллельным лучам через весь корпус.

    Окно может быть в любом месте корпуса: верх — максимум 3×3 по всем лучам, низ — минимум.
    Вне листов GLO-30: клетка 1° с сушей (по списку листов Copernicus DEM) — рельеф неизвестен,
    верх до REGION_MAX_M (`beyond`); клетка без суши — море, уровень 0."""
    import numpy as np

    key = (bid, round(az * 4) / 4, max_d)
    if key in scene.far_cache:
        return scene.far_cache[key]
    geom = scene.osm[bid]["geom"]
    c = geom.centroid
    ux, uy = _unit(key[1])
    w = _half_width(geom, key[1])
    offsets = np.linspace(-w, w, max(1, math.ceil(2 * w / FAR_LATERAL_M) + 1))
    d = np.concatenate([np.arange(NEAR_M, 20_000, 30.0), np.arange(20_000, FAR_MAX_M, 90.0)])
    d = d[d < max_d]
    n = len(d)
    lo = np.full(n, np.inf)
    hi = np.full(n, -np.inf)
    mid = np.zeros(n)
    beyond = np.zeros(n, bool)
    land = np.array(sorted(la * 1000 + lo_ for la, lo_ in scene.land_cells))
    for k, off in enumerate(offsets):
        lon0, lat0 = scene.inverse.transform(c.x + uy * off, c.y - ux * off)
        lon, lat, _ = scene.geod.fwd(np.full(n, lon0), np.full(n, lat0), np.full(n, key[1]), d)
        raw, mn, mx, ok = scene.dsm.sample(lon, lat)
        cell = np.floor(lat).astype(int) * 1000 + np.floor(lon).astype(int)
        unknown = ~ok & np.isin(cell, land)  # суша без данных о рельефе
        beyond |= unknown
        lo = np.fmin(lo, np.where(ok, mn - DSM_SIGMA_M, np.where(unknown, -np.inf, 0.0)))
        hi = np.fmax(hi, np.where(ok, mx + DSM_SIGMA_M, np.where(unknown, REGION_MAX_M, 0.0)))
        if k == len(offsets) // 2:
            mid = np.where(ok, raw, np.where(unknown, np.nan, 0.0))
    prof = (d, lo, hi, mid, beyond)
    scene.far_cache[key] = prof
    return prof


def ray(scene: Scene, bid: str, x, y, az, g_w, max_d: float = FAR_MAX_M) -> Obstacles:
    """Все препятствия луча: ближние — от точки окна, дальние — по всему корпусу."""
    import numpy as np

    near = near_obstacles(scene, x, y, az, g_w, min(BUILDINGS_M, max_d))
    fd, flo, fhi, fmid, fbeyond = far_profile(scene, bid, az, max_d)
    c = scene.osm[bid]["geom"].centroid
    ux, uy = _unit(az)
    # Дальние отсчёты — по лучам из центра корпуса и параллельным ему: вдоль луча окно
    # от них дальше или ближе ровно на свой вынос относительно центра.
    shift = (x - c.x) * ux + (y - c.y) * uy
    return Obstacles(
        np.concatenate([near.d, fd - shift]),
        np.concatenate([near.lo, flo]),
        np.concatenate([near.hi, fhi]),
        np.concatenate([near.mid, fmid]),
        near.kind + ["beyond" if b else "terrain" for b in fbeyond],
        near.ids + ["copdem_tile_missing" if b else "copdem" for b in fbeyond],
    )


def sun_thresholds(obs: Obstacles, alt_deg: float) -> tuple[float, float, int, int, float, int]:
    """Глаз выше E_clear — Солнце точно видно; ниже E_block — точно закрыто. И кто решает.

    Последнее — порог для оценки по центральным значениям (NaN — оценки нет)."""
    import numpy as np

    rise = _rise(np.maximum(obs.d, 0.0), alt_deg)
    need, block = obs.hi - rise, obs.lo - rise
    i, j = int(np.argmax(need)), int(np.argmax(block))
    mid = obs.mid - rise
    if "beyond" in obs.kind or np.isnan(mid).all():
        return float(need[i]), float(block[j]), i, j, math.nan, -1
    k = int(np.nanargmax(mid))
    return float(need[i]), float(block[j]), i, j, float(mid[k]), k


def sea_runs(scene: Scene, x: float, y: float, az: float):
    """От точки окна: участки моря на луче — вода на уровне моря не короче SEA_RUN_M.

    None — до моря нет данных WorldCover: «моря нет» сказать нельзя."""
    import numpy as np

    lon0, lat0 = scene.inverse.transform(x, y)
    d = np.arange(SEA_STEP_M, SEA_MAX_M, SEA_STEP_M)
    n = len(d)
    lon, lat, _ = scene.geod.fwd(np.full(n, lon0), np.full(n, lat0), np.full(n, az), d)
    cls, _, _, covered = scene.worldcover.sample(lon, lat)
    level, _, _, _ = scene.dsm.sample(lon, lat)
    sea = (cls == WATER) & covered & ~(level > SEA_LEVEL_M)
    run = int(SEA_RUN_M / SEA_STEP_M)
    runs = []
    k = 0
    while k < n:
        if not sea[k]:
            k += 1
            continue
        end = k
        while end + 1 < n and sea[end + 1]:
            end += 1
        if end - k + 1 >= run:
            runs.append((float(d[k]), float(d[end])))
        k = end + 1
    first_gap = np.flatnonzero(~covered)
    if not runs and len(first_gap):
        return None
    return runs


@dataclass
class SeaRay:
    e_clear: float
    e_block: float
    need_i: int  # кто решает «да» (индекс в obs)
    block_i: int  # кто закрывает наверняка
    obs: Obstacles | None
    sea: bool | None  # есть море на луче; None — нет данных
    e_mid: float = math.nan  # порог для оценки


def sea_ray(scene: Scene, bid, x, y, az, g_w) -> SeaRay:
    """Луч к морю: виден ли из окна дальний конец какого-нибудь участка моря.

    Для каждого участка препятствия — до его начала (вода сама не препятствие); к дальнему концу
    линия взгляда идёт выше всего. «Да» — виден хотя бы один участок."""
    import numpy as np

    runs = sea_runs(scene, x, y, az)
    if runs is None:
        return SeaRay(math.inf, -math.inf, -1, -1, None, None)
    if not runs:
        return SeaRay(math.inf, math.inf, -1, -1, None, False)
    obs = ray(scene, bid, x, y, az, g_w, max_d=runs[-1][0] + NEAR_M)
    best = SeaRay(math.inf, math.inf, -1, -1, obs, True, math.inf)
    for start, t in runs:
        keep = np.flatnonzero(obs.d < start)
        if not len(keep):
            return SeaRay(-math.inf, -math.inf, -1, -1, obs, True, -math.inf)
        xs = np.maximum(obs.d[keep], 0.0)
        frac = 1 - xs / t
        corr = -_drop(xs) + _drop(t) * xs / t
        need = (obs.hi[keep] + corr) / frac
        block = (obs.lo[keep] + corr) / frac
        mid = (obs.mid[keep] + corr) / frac
        i, j = int(np.argmax(need)), int(np.argmax(block))
        e_mid = float(np.nanmax(mid)) if not np.isnan(mid).all() else -math.inf
        if float(need[i]) < best.e_clear:
            best.e_clear, best.need_i = float(need[i]), int(keep[i])
        if float(block[j]) < best.e_block:
            best.e_block, best.block_i = float(block[j]), int(keep[j])
        best.e_mid = min(best.e_mid, e_mid)
    return best


# --- Окна ---


def _front(az: float, normal: float) -> float:
    return abs((az - normal + 180) % 360 - 180)


@dataclass
class Cell:
    """Итог по окну на этаже: да / нет / неизвестно и чем объяснить."""

    visible: bool | None
    reason: str | None = None  # для «неизвестно»
    why: str | None = None  # для «да» / «нет»: clear, sun_behind_facade, blocked, no_sea…
    blocker: str | None = None  # кто закрывает («нет») или от чьей высоты зависит («неизвестно»)
    blocker_kind: str | None = None


def _combine(points: list[Cell]) -> Cell:
    """Окно может быть в любом месте фасада: ответ один во всех точках — иначе неизвестно."""
    vals = {p.visible for p in points}
    if vals == {True}:
        return points[0]
    if vals == {False}:
        why = Counter(p.why for p in points).most_common(1)[0][0]
        return next(p for p in points if p.why == why)
    if {True, False} <= vals:
        return Cell(None, "view_window_position")
    reason = Counter(p.reason for p in points if p.visible is None).most_common(1)[0][0]
    return next(p for p in points if p.reason == reason)


def _decider(obs: Obstacles, i: int, reason: str | None = None) -> Cell:
    kind = obs.kind[i]
    return Cell(
        None,
        reason or DECIDER_REASONS.get(kind, "view_obstacle_height"),
        blocker=obs.ids[i],
        blocker_kind=kind,
    )


def sun_track(lat: float, lon: float, day: date, tz: str):
    """Минуты перед закатом с шагом STEP_MINUTES: (азимут, видимая высота верхнего края)."""
    import pandas as pd
    import pvlib

    times = pd.date_range(f"{day} 12:00", f"{day} 23:59", freq="1min", tz=tz)
    pos = pvlib.solarposition.get_solarposition(times, lat, lon, method="nrel_numpy")
    si = int((pos["elevation"] <= SUNSET_ELEVATION).to_numpy().argmax())
    picks = sorted({*range(si - LAST_MINUTES, si, STEP_MINUTES), si - 1})
    track = [
        (float(pos["azimuth"].iloc[k]), float(pos["apparent_elevation"].iloc[k]) + UPPER_LIMB_DEG)
        for k in picks
    ]
    return track, times[si].isoformat(timespec="minutes")


@dataclass
class Window:
    hotel_id: str
    building_id: str
    facade: dict[str, Any]
    floors: int | None


def use_pack_buildings(scene: Scene, buildings: Iterable[dict[str, Any]]) -> int:
    """Корпуса отелей — из пакета города (каталог №26, новее сцены v9): контур и теги заменяют
    одноимённые здания сцены, новые добавляются. Так фасады пакета и «своё здание» среди
    препятствий — одна геометрия. Возвращает число добавленных зданий."""
    import numpy as np
    import shapely

    def metric(g):
        return shapely.transform(
            g, lambda xy: np.column_stack(scene.project.transform(xy[:, 0], xy[:, 1]))
        )

    added = []
    for b in buildings:
        gm = metric(shapely.geometry.shape(b["geometry"]))
        gm = gm if gm.is_valid else gm.buffer(0)
        tags = b.get("tags") or {}
        old = scene.osm.get(b["id"])
        lo, hi, kind = top_interval(tags, old and old.get("globfp"), scene.curated.get(b["id"]))
        mid = top_central(tags, old and old.get("globfp"), scene.curated.get(b["id"]))
        if old is not None:
            i = old["index"]
            old.update(tags=tags, geom=gm)
            scene.polys[i] = gm
            scene.top_lo[i], scene.top_hi[i], scene.top_mid[i], scene.kind[i] = lo, hi, mid, kind
            scene.ground_lo[i] = scene.ground_mid[i] = scene.ground_hi[i] = np.nan
            scene.cap[i] = np.nan
        else:
            added.append((b["id"], gm, tags, lo, hi, mid, kind))
    if added:
        n = len(scene.polys)
        scene.polys = np.concatenate([scene.polys, np.array([a[1] for a in added], dtype=object)])
        scene.top_lo = np.concatenate([scene.top_lo, [a[3] for a in added]])
        scene.top_hi = np.concatenate([scene.top_hi, [a[4] for a in added]])
        scene.top_mid = np.concatenate([scene.top_mid, [a[5] for a in added]])
        for arr in ("ground_lo", "ground_mid", "ground_hi", "cap"):
            setattr(scene, arr, np.concatenate([getattr(scene, arr), np.full(len(added), np.nan)]))
        for k, (bid, gm, tags, *_rest) in enumerate(added):
            scene.kind.append(_rest[-1])
            scene.ids.append(bid)
            scene.osm[bid] = {"tags": tags, "geom": gm, "version": None, "edited_at": None,
                              "globfp": None, "index": n + k}  # fmt: skip
    scene.tree = shapely.STRtree(scene.polys)
    return len(added)


def pack_facade(scene: Scene, row: dict[str, Any]) -> dict[str, Any]:
    """Фасад из каталога пакета (`facades.jsonl.gz`, ID `seg-…`) — в метрах сцены.

    Внешняя нормаль — перпендикуляр к отрезку в сторону азимута фасада из каталога."""
    (lon_a, lat_a), (lon_b, lat_b) = row["geometry"]["coordinates"][:2]
    (ax, bx), (ay, by) = scene.project.transform([lon_a, lon_b], [lat_a, lat_b])
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    nx, ny = dy / length, -dx / length
    ux, uy = _unit(float(row["azimuth_deg"]))
    if nx * ux + ny * uy < 0:
        nx, ny = -nx, -ny
    return {
        "facade_id": row["facade_id"],
        "a": (ax, ay),
        "b": (bx, by),
        "normal": (nx, ny),
        "azimuth": math.degrees(math.atan2(nx, ny)) % 360,
        "length": length,
        "courtyard": bool(row.get("is_courtyard")),
        "side": row.get("side"),
    }


def windows_from_pack(
    scene: Scene, facades: Iterable[dict[str, Any]], links: Iterable[dict[str, Any]]
) -> Iterator[Window]:
    """Окна по каталогу фасадов пакета (D41: общая схема ID — `seg-…`), отрезки от 3 м."""
    by_building: dict[str, list[dict[str, Any]]] = {}
    for f in facades:
        if (f.get("length_m") or 0) >= MIN_FACADE_M:
            by_building.setdefault(f["building_id"], []).append(f)
    for link in links:
        bid = link["building_id"]
        b = scene.osm.get(bid)
        if b is None:
            continue
        floors = floors_of(b["tags"], scene.curated.get(bid))
        for row in by_building.get(bid, []):
            yield Window(str(link["hotel_id"]), bid, pack_facade(scene, row), floors)


def windows_of(scene: Scene, matches: Iterable[dict[str, Any]]) -> Iterator[Window]:
    for m in matches:
        b = scene.osm.get(m["osm_id"])
        if b is None:
            continue
        floors = floors_of(b["tags"], scene.curated.get(m["osm_id"]))
        for side in facade_segments(b["geom"]):
            if side["length"] >= MIN_FACADE_M:
                yield Window(str(m["hotel_id"]), m["osm_id"], side, floors)


def _points(w: Window) -> list[tuple[float, float]]:
    """Точки окна вдоль фасада: от 0,3 м от углов, не реже SAMPLE_STEP_M, в 0,3 м от стены."""
    (ax, ay), (bx, by), (nx, ny) = w.facade["a"], w.facade["b"], w.facade["normal"]
    inset = 0.3 / w.facade["length"]
    n = max(2, math.ceil((w.facade["length"] - 0.6) / SAMPLE_STEP_M) + 1)
    ts = [inset + (1 - 2 * inset) * k / (n - 1) for k in range(n)]
    return [(ax + (bx - ax) * t + nx * 0.3, ay + (by - ay) * t + ny * 0.3) for t in ts]


def _floors(w: Window) -> list[int | None]:
    return list(range(1, w.floors + 1)) if w.floors else [None]


def _minutes(scene: Scene, w: Window, track, x, y, g_w) -> list[tuple]:
    out = []
    for az, alt in track:
        front = _front(az, w.facade["azimuth"])
        if front >= FRONT_NO_DEG:
            out.append(("behind", None))
            continue
        obs = ray(scene, w.building_id, x, y, az, g_w)
        state = "oblique" if front > FRONT_YES_DEG else "front"
        out.append((state, (*sun_thresholds(obs, alt), obs)))
    return out


def v05_cells(scene: Scene, w: Window, track, g_w) -> dict[int | None, tuple]:
    """Закат по этажам фасада. По этажу — (расчёт, оценка); оценка — None, если её нет."""
    per_floor: dict[int | None, list[tuple]] = {f: [] for f in _floors(w)}
    for x, y in _points(w):
        minutes = _minutes(scene, w, track, x, y, g_w)
        for f in per_floor:
            per_floor[f].append((_v05_point(minutes, f, g_w), _v05_estimate(minutes, f, g_w)))
    return {f: _pair(cs) for f, cs in per_floor.items()}


def _pair(points: list[tuple]) -> tuple[Cell, Cell | None]:
    est = [e for _, e in points]
    same = est[0] is not None and len({e.visible if e else None for e in est}) == 1
    return _combine([c for c, _ in points]), est[0] if same else None


def _v05_point(minutes, floor: int | None, g_w: tuple) -> Cell:
    """«Да» — диск точно виден все последние минуты; «нет» — точно ни минуты (D40).

    Так ответ верен при любом прочтении «виден в последние 30 минут»: и «всё это время», и
    «хотя бы минуту» (каталог, v9). Видно часть времени — «неизвестно» (`view_sun_partial`)."""
    if all(state == "behind" for state, _ in minutes):
        return Cell(False, why="sun_behind_facade")
    if floor is None:
        return Cell(None, "floor_count_unknown")
    e_lo, e_hi = eye_interval(floor)
    eye_lo, eye_hi = g_w[0] + e_lo, g_w[2] + e_hi
    seen, hidden, undecided = 0, 0, None
    blocker = None
    for state, data in minutes:
        if state == "behind":
            hidden += 1
            continue
        e_clear, e_block, i, j, _, _, obs = data
        if eye_hi < e_block:
            hidden += 1
            blocker = blocker or (obs.ids[j], obs.kind[j])
        elif state == "front" and eye_lo > e_clear and e_lo >= LOW_EYE_M:
            seen += 1
        elif undecided is None:
            if eye_lo <= e_clear:
                undecided = _decider(obs, i)
            elif state == "oblique":
                undecided = Cell(None, "view_sun_oblique")
            else:
                undecided = Cell(None, "view_low_floor")
    if seen == len(minutes):
        return Cell(True, why="clear")
    if hidden == len(minutes):
        if blocker is None:
            return Cell(False, why="sun_behind_facade")
        return Cell(False, why="blocked", blocker=blocker[0], blocker_kind=blocker[1])
    if undecided is None or (seen and hidden):
        return Cell(None, "view_sun_partial")
    return undecided


def _v05_estimate(minutes, floor: int | None, g_w: tuple) -> Cell | None:
    """Оценка (D18): этаж 3,3 м, соседи по центру интервала, земля — улица по GLO-30."""
    if all(state == "behind" for state, _ in minutes):
        return Cell(False, why="sun_behind_facade")
    if floor is None:
        return None
    eye = g_w[1] + eye_central(floor)
    seen, hidden, blocker = 0, 0, None
    for state, data in minutes:
        if state == "behind":
            hidden += 1
            continue
        *_, e_mid, k, obs = data
        if math.isnan(e_mid):
            return None
        if eye > e_mid:
            seen += 1
        else:
            hidden += 1
            blocker = blocker or (obs.ids[k], obs.kind[k])
    if seen == len(minutes):
        return Cell(True, why="clear")
    if hidden == len(minutes):
        return Cell(False, why="blocked", blocker=blocker[0] if blocker else None,
                    blocker_kind=blocker[1] if blocker else None)  # fmt: skip
    return None  # по оценке — часть времени: оценки «да / нет» нет


def v05_all_year(winter, summer, normal: float) -> Cell:
    """Без даты: «нет», только если Солнце за плоскостью фасада все последние минуты весь год.

    За год эти минуты заходят от азимута декабрьского начала до июньского конца; дуга «перед
    фасадом» шириной 180° внутрь этого сектора (~72°) не помещается — хватает проверить концы."""
    edges = [winter[0][0], winter[-1][0], summer[0][0], summer[-1][0]]
    if all(_front(az, normal) >= FRONT_NO_DEG for az in edges):
        return Cell(False, why="sun_behind_facade_all_year")
    return Cell(None, "view_need_dates")


def v01_cells(scene: Scene, w: Window, g_w) -> dict[int | None, tuple]:
    """Море по этажам фасада: «да» — в секторе ±SECTOR_DEG точно видна вода моря."""
    import numpy as np

    normal = w.facade["azimuth"]
    per_floor: dict[int | None, list[tuple]] = {f: [] for f in _floors(w)}
    azs = normal + np.arange(-SECTOR_DEG, SECTOR_DEG + 0.01, SECTOR_STEP_DEG)
    for x, y in _points(w):
        rays = [sea_ray(scene, w.building_id, x, y, float(a % 360), g_w) for a in azs]
        for f in per_floor:
            per_floor[f].append((_v01_point(rays, f, g_w), _v01_estimate(rays, f, g_w)))
    return {f: _pair(cs) for f, cs in per_floor.items()}


def _v01_point(rays: list[SeaRay], floor: int | None, g_w: tuple) -> Cell:
    """«Нет» — только если в секторе нет моря совсем (и данные есть на всех лучах).

    Море закрыто зданиями — «неизвестно» с оценкой рядом: лучи идут через градус, щель между
    домами между ними не доказать."""
    if all(r.sea is False for r in rays):
        return Cell(False, why="no_sea")
    sea = [r for r in rays if r.sea]
    if not sea:
        return Cell(None, "view_landcover_missing")
    if floor is None:
        return Cell(None, "floor_count_unknown")
    e_lo, e_hi = eye_interval(floor)
    eye_lo, eye_hi = g_w[0] + e_lo, g_w[2] + e_hi
    if any(eye_lo > r.e_clear for r in sea):
        return Cell(True, why="clear") if e_lo >= LOW_EYE_M else Cell(None, "view_low_floor")
    if all(eye_hi < r.e_block for r in sea):
        r = min(sea, key=lambda r: r.e_block)
        return Cell(None, "view_sea_blocked", blocker=r.obs.ids[r.block_i],
                    blocker_kind=r.obs.kind[r.block_i])  # fmt: skip
    best = min(sea, key=lambda r: r.e_clear)
    return _decider(best.obs, best.need_i)


def _v01_estimate(rays: list[SeaRay], floor: int | None, g_w: tuple) -> Cell | None:
    if all(r.sea is False for r in rays):
        return Cell(False, why="no_sea")
    sea = [r for r in rays if r.sea]
    if not sea or floor is None:
        return None
    eye = g_w[1] + eye_central(floor)
    if any(eye > r.e_mid for r in sea):
        return Cell(True, why="clear")
    return Cell(False, why="blocked")


# --- Факты ---


def _sources(scene: Scene, building_id: str, cell: Cell) -> list[Source]:
    b = scene.osm[building_id]
    out = [
        Source(
            provider="OpenStreetMap",
            url=URLS["osm"] + building_id,
            license="ODbL-1.0",
            dataset_version=f"v{b['version']} {b['edited_at']}",
        ),
        Source(
            provider="Copernicus DEM GLO-30",
            url=URLS["copdem"],
            license="Copernicus DEM licence (free, attribution)",
            dataset_version="2022 COG",
        ),
        Source(
            provider="ESA WorldCover",
            url=URLS["worldcover"],
            license="CC BY 4.0",
            dataset_version="WorldCover2021v200",
        ),
    ]
    if cell.blocker_kind in ("globfp", "globfp_only"):
        out.append(
            Source(
                provider="3D-GloBFP",
                url=URLS["globfp"],
                license="CC BY 4.0",
                dataset_version="v2 (2025-05-22)",
            )
        )
    if cell.blocker in scene.curated:
        r = scene.curated[cell.blocker]
        out.append(
            Source(
                provider=f"Команда: {r.get('source') or 'карты'} (D33)",
                url=r["source_url"],
                license="проверено человеком",
                dataset_version=f"{r['checked_by']} {r['checked_at']}",
            )
        )
    return out


def make_fact(
    scene: Scene,
    w: Window,
    feature_id: str,
    params: dict[str, Any],
    cell: Cell,
    floor: int | None,
    city_id: str,
    g_w: tuple[float, float, float],
    calculated_at: datetime,
    sunset: str | None = None,
    estimate_of: Cell | None = None,
) -> Fact:
    """Факт окна: расчёт (computed / unknown) или оценка (`estimate_of` — ячейка расчёта)."""
    from core.reasons import reason_text

    if floor is None:
        level, subject = "facade", f"{w.building_id}:{w.facade['facade_id']}"
    else:
        level = "facade_physical_floor"
        subject = f"{w.building_id}:{w.facade['facade_id']}:floor{floor}"
    value = None
    if cell.visible is not None:
        value = {"visible": cell.visible, "why": cell.why}
        if cell.blocker:
            value["blocker"] = cell.blocker
    detail: dict[str, Any] = {
        "facade_azimuth_deg": round(w.facade["azimuth"], 1),
        "facade_length_m": round(w.facade["length"], 1),
        "ground_m": [round(g, 1) for g in g_w],
        "floors": w.floors,
    }
    if floor is not None:
        detail["eye_height_m"] = (
            round(eye_central(floor), 1)
            if estimate_of
            else [round(v, 1) for v in eye_interval(floor)]
        )
    if sunset:
        detail["sunset_local"] = sunset
    if cell.visible is None and cell.blocker:
        detail["decided_by"] = cell.blocker
        detail["decided_by_kind"] = cell.blocker_kind
    estimate = estimate_of is not None
    version = f"{VERSION}/estimate" if estimate else VERSION
    if estimate:
        status = Status.estimate
        confidence = Confidence(
            level="low",
            reason=(
                "Оценка: этаж 3,3 м, высота соседей — середина интервала, земля — улица по "
                "GLO-30. Для обязательного условия её мало (D18)."
            ),
        )
        attempts = [
            RecipeAttempt(
                recipe_id=f"{feature_id}-R1",
                data_kind="Б",
                state="failed",
                reason=estimate_of.reason,  # почему интервальный расчёт не дал ответа
            ),
            RecipeAttempt(
                recipe_id=f"{feature_id}-R1-estimate",
                data_kind="Б",
                state="accepted",
                reason="центральные значения (D18)",
            ),
        ]
    elif value is not None:
        status = Status.computed
        confidence = Confidence(
            level="medium",
            reason=(
                "Интервальный расчёт лучей: ответ один при любой высоте препятствий, земли и "
                "этажа в пределах погрешности данных и в любом месте фасада. Облака, туман и "
                "постройки новее снимка OSM не учтены."
            ),
        )
        attempts = [RecipeAttempt(recipe_id=f"{feature_id}-R1", data_kind="Б", state="accepted")]
    else:
        status = Status.unknown
        confidence = Confidence(level="unknown", reason=reason_text(cell.reason))
        attempts = [
            RecipeAttempt(
                recipe_id=f"{feature_id}-R1", data_kind="Б", state="failed", reason=cell.reason
            )
        ]
    attempts.append(
        RecipeAttempt(
            recipe_id=f"{feature_id}-R2",
            data_kind="В",
            state="skipped",
            reason="Нет датированного фото из окна категории.",
        )
    )
    return Fact(
        fact_id=make_fact_id(w.hotel_id, subject, feature_id, params, version),
        hotel_id=w.hotel_id,
        city_id=city_id,
        subject_level=level,
        subject_id=subject,
        feature_id=feature_id,
        params=params,
        value=value,
        status=status,
        unknown_reason=cell.reason if value is None else None,
        sources=_sources(scene, w.building_id, cell),
        recipe_id=f"{feature_id}-R1",
        algorithm_version=version,
        confidence=confidence,
        calculated_at=calculated_at,
        scope_note=(
            f"Окно — любое место этого фасада (точки через {SAMPLE_STEP_M:g} м) на этом этаже. "
            "Номер категории — только по ответу отеля, где она расположена (D17)."
        ),
        recipe_attempts=attempts,
        depends_on=[detail],
        hard_condition_eligible=False if estimate else (True if value is not None else None),
    )


# Причины «неизвестно», при которых оценки нет: решает не высота, а то, чего в данных нет.
NO_ESTIMATE = frozenset(
    {"view_terrain_beyond", "floor_count_unknown", "view_landcover_missing", "view_need_dates"}
)


def facts_for_window(
    scene: Scene,
    w: Window,
    dates: list[date],
    season: tuple[date, date],
    tz: str,
    city_id: str,
    calculated_at: datetime,
    suns: dict | None = None,
) -> list[Fact]:
    """Факты V05 (по датам и «в любую дату») и V01 одного фасада.

    Один факт на фасад — только если ответ не зависит от этажа (`FLOOR_FREE`) или этаж
    неизвестен; иначе по этажам 1…N: этаж выше посчитанных не получает чужого «нет».
    Где расчёт дал «неизвестно», рядом — оценка (D18): объясняет и ранжирует, но
    обязательное условие не закрывает."""
    b = scene.osm[w.building_id]
    i = b["index"]
    ensure_ground(scene, [i])
    g_w = (float(scene.ground_lo[i]), float(scene.ground_mid[i]), float(scene.ground_hi[i]))
    if not math.isfinite(g_w[0]):
        return []
    c = b["geom"].centroid
    lon, lat = scene.inverse.transform(c.x, c.y)
    suns = {} if suns is None else suns

    def track(d: date):
        if (w.building_id, d) not in suns:
            suns[w.building_id, d] = sun_track(lat, lon, d, tz)
        return suns[w.building_id, d]

    jobs = []
    for d in dates:
        t, sunset = track(d)
        cells = v05_cells(scene, w, t, g_w)
        jobs.append(("V05", V05_PARAMS | {"date": d.isoformat()}, cells, sunset))
    year = v05_all_year(track(season[0])[0], track(season[1])[0], w.facade["azimuth"])
    jobs.append(("V05", dict(V05_PARAMS), {None: (year, None)}, None))
    jobs.append(("V01", V01_PARAMS, v01_cells(scene, w, g_w), None))
    out = []
    for fid, params, cells, sunset in jobs:
        keyed = {}
        for floor, (cell, est) in cells.items():
            if cell.visible is not None or cell.reason in NO_ESTIMATE:
                est = None
            keyed[floor] = (cell, est)
        same = {
            (c.visible, c.reason, c.why, c.blocker, e.visible if e else None)
            for c, e in keyed.values()
        }
        first = next(iter(keyed.values()))[0]
        floor_free = first.why in FLOOR_FREE or (
            first.visible is None and first.reason in NO_ESTIMATE
        )
        if None in keyed or (len(same) == 1 and floor_free):
            floors = [None]
        else:
            floors = list(keyed)
        for floor in floors:
            cell, est = keyed[floor] if floor is not None else next(iter(keyed.values()))
            out.append(
                make_fact(scene, w, fid, params, cell, floor, city_id, g_w, calculated_at, sunset)
            )
            if est is not None:
                out.append(
                    make_fact(
                        scene, w, fid, params, est, floor, city_id, g_w, calculated_at, sunset, cell
                    )
                )
    return out


# --- Часы прямого солнца на окне (S01 за день, S02 в местном интервале): верхняя граница ---
# Солнце перед плоскостью фасада и выше рельефа по GLO-30 (нижняя граница высот поверхности) при
# глазе на самом верху корпуса. Соседние дома и деревья ближе NEAR_M не учтены: они только
# убавляют солнце. Поэтому «не больше N часов» по такой границе — точное «да», «не меньше N» при
# границе меньше N — точное «нет»; остальное — «неизвестно» с наклоном (core/search.py).
SUN_HOURS_VERSION = "sun-hours-upper@1"
SUN_MONTHS = (5, 6, 7, 8, 9, 11)  # тёплый сезон и даты проверки трека (ноябрь)
# Интервалы — ровно те, что ставит разбор (agents/search/rules.py, правило sun_hours).
SUN_INTERVALS = (("07:00", "10:00"), ("07:00", "11:00"), ("11:00", "16:00"), ("13:00", "18:00"))
SUN_STEP_MIN = 10
SUN_AZ_BIN_DEG = 2.0
SUN_HORIZON_M = 60_000.0  # горы дальше 60 км закрывают Солнце только у самого горизонта


def sun_samples(lat: float, lon: float, day: date, tz: str) -> list[tuple[str, float, float]]:
    """(местное «ЧЧ:ММ», азимут, видимая высота центра диска) с шагом SUN_STEP_MIN, днём."""
    import pandas as pd
    import pvlib

    times = pd.date_range(f"{day} 00:00", f"{day} 23:59", freq=f"{SUN_STEP_MIN}min", tz=tz)
    pos = pvlib.solarposition.get_solarposition(times, lat, lon, method="nrel_numpy")
    return [
        (t.strftime("%H:%M"), float(az), float(alt))
        for t, az, alt in zip(times, pos["azimuth"], pos["apparent_elevation"], strict=True)
        if alt > 0
    ]


def sun_possible(scene: Scene, bid: str, samples, eye_top: float) -> list[bool]:
    """Может ли Солнце в этот момент быть над рельефом для глаза на верху корпуса."""
    import numpy as np

    out = []
    for _, az, alt in samples:
        az_bin = round(az / SUN_AZ_BIN_DEG) * SUN_AZ_BIN_DEG
        d, lo, _, _, _ = far_profile(scene, bid, az_bin, SUN_HORIZON_M)
        rise = _rise(np.maximum(d, 0.0), alt)
        block = float(np.max(lo - rise)) if len(d) else -math.inf
        out.append(eye_top > block)
    return out


def sun_hours_facts(
    scene: Scene,
    windows: list[Window],
    tz: str,
    city_id: str,
    calculated_at: datetime,
    year: int = 2027,
) -> list[Fact]:
    """S01 (часы за день месяца) и S02 (часы в интервалах разбора) по фасадам одного корпуса."""
    if not windows:
        return []
    bid = windows[0].building_id
    b = scene.osm[bid]
    ensure_ground(scene, [b["index"]])
    g_hi = float(scene.ground_hi[b["index"]])
    if not math.isfinite(g_hi):
        return []
    floors = windows[0].floors
    eye_top = g_hi + (eye_interval(floors)[1] if floors else UNKNOWN_TOP_M[1])
    c = b["geom"].centroid
    lon, lat = scene.inverse.transform(c.x, c.y)
    by_month = {}
    for month in SUN_MONTHS:
        samples = sun_samples(lat, lon, date(year, month, 15), tz)
        by_month[month] = (samples, sun_possible(scene, bid, samples, eye_top))
    out = []
    step_h = SUN_STEP_MIN / 60
    for w in windows:
        normal = w.facade["azimuth"]
        for month, (samples, possible) in by_month.items():
            lit = [
                t
                for (t, az, _), ok in zip(samples, possible, strict=True)
                if ok and _front(az, normal) < FRONT_NO_DEG
            ]
            jobs = [("S01", {"month": month}, len(lit))]
            for start, end in SUN_INTERVALS:
                n = sum(start <= t < end for t in lit)
                jobs.append(("S02", {"month": month, "time_from": start, "time_to": end}, n))
            for fid, params, n in jobs:
                out.append(_sun_fact(scene, w, fid, params, round(n * step_h, 2), city_id,
                                     calculated_at, floors))  # fmt: skip
    return out


def _sun_fact(
    scene: Scene,
    w: Window,
    fid: str,
    params: dict[str, Any],
    hours: float,
    city_id: str,
    calculated_at: datetime,
    floors: int | None,
) -> Fact:
    subject = f"{w.building_id}:{w.facade['facade_id']}"
    return Fact(
        fact_id=make_fact_id(w.hotel_id, subject, fid, params, SUN_HOURS_VERSION),
        hotel_id=w.hotel_id,
        city_id=city_id,
        subject_level="facade",
        subject_id=subject,
        feature_id=fid,
        params=params,
        value={"hours": hours, "bound": "upper"},
        unit="h",
        status=Status.computed,
        sources=_sources(scene, w.building_id, Cell(None)),
        recipe_id=f"{fid}-R1",
        algorithm_version=SUN_HOURS_VERSION,
        confidence=Confidence(
            level="medium",
            reason=(
                "Верхняя граница: Солнце перед фасадом и над рельефом GLO-30 для глаза на верху "
                "корпуса, середина месяца, шаг 10 минут. Соседние дома и деревья только убавят."
            ),
        ),
        calculated_at=calculated_at,
        scope_note="Любое окно этого фасада; номер категории — по ответу отеля (D17).",
        recipe_attempts=[RecipeAttempt(recipe_id=f"{fid}-R1", data_kind="Б", state="accepted")],
        depends_on=[{"facade_azimuth_deg": round(w.facade["azimuth"], 1), "floors": floors}],
        hard_condition_eligible=True,
    )


# --- Вид на горы (V02): «нет» — точно, «скорее да» — оценка ---
# В секторе окна ±45° ищем склон выше MOUNTAIN_MIN_M, который для глаза на верху корпуса
# поднимается над рельефом между ними (рельеф-заслон — нижняя граница высот GLO-30, склон —
# верхняя). Не нашли ни в одном направлении — гор из окна не видно ни при каких соседях: «нет»,
# расчёт. Нашли — горы могут быть видны, но соседние дома и деревья мы тут не считаем: оценка
# «да» (D18) — ранжирует и объясняет, обязательное условие не закрывает.
MOUNTAIN_VERSION = "view-mountains@1"
MOUNTAIN_MIN_M = 500.0
MOUNTAIN_MAX_M = 60_000.0
MOUNTAIN_STEP_DEG = 3.0


def mountain_seen(scene: Scene, bid: str, az: float, eye_top: float) -> float | None:
    """Высота самого видного склона выше MOUNTAIN_MIN_M в направлении az (или None)."""
    import numpy as np

    d, lo, hi, _, beyond = far_profile(scene, bid, az, MOUNTAIN_MAX_M)
    if not len(d):
        return None
    curv = d * d * (1 - REFRACTION_K) / (2 * EARTH_R)
    block = np.maximum.accumulate((lo - curv - eye_top) / np.maximum(d, 1.0))  # тангенс заслона
    # За краем листов GLO-30 высота — заглушка REGION_MAX_M, а не склон: горой её не считаем.
    seen = (hi > MOUNTAIN_MIN_M) & ~beyond & ((hi - curv - eye_top) / np.maximum(d, 1.0) >= block)
    return float(np.max(hi[seen])) if seen.any() else None


def mountain_facts(
    scene: Scene, windows: list[Window], city_id: str, calculated_at: datetime
) -> list[Fact]:
    """V02 по фасадам одного корпуса: «нет» расчётом или «да» оценкой."""
    if not windows:
        return []
    bid = windows[0].building_id
    b = scene.osm[bid]
    ensure_ground(scene, [b["index"]])
    g_hi = float(scene.ground_hi[b["index"]])
    if not math.isfinite(g_hi):
        return []
    floors = windows[0].floors
    eye_top = g_hi + (eye_interval(floors)[1] if floors else UNKNOWN_TOP_M[1])
    cache: dict[float, float | None] = {}
    out = []
    for w in windows:
        normal = w.facade["azimuth"]
        steps = int(SECTOR_DEG // MOUNTAIN_STEP_DEG)
        peaks = []
        for k in range(-steps, steps + 1):
            az = (
                round(((normal + k * MOUNTAIN_STEP_DEG) % 360) / MOUNTAIN_STEP_DEG)
                * MOUNTAIN_STEP_DEG
            )
            if az not in cache:
                cache[az] = mountain_seen(scene, bid, az, eye_top)
            if cache[az] is not None:
                peaks.append(cache[az])
        out.append(_mountain_fact(scene, w, peaks, city_id, calculated_at, floors))
    return out


def _mountain_fact(
    scene: Scene,
    w: Window,
    peaks: list[float],
    city_id: str,
    calculated_at: datetime,
    floors: int | None,
) -> Fact:
    subject = f"{w.building_id}:{w.facade['facade_id']}"
    seen = bool(peaks)
    status = Status.estimate if seen else Status.computed
    value: dict[str, Any] = {
        "visible": seen,
        "why": "mountains_in_sector" if seen else "no_mountains",
    }
    if seen:
        value["highest_m"] = round(max(peaks))
    version = MOUNTAIN_VERSION + ("/estimate" if seen else "")
    reason = (
        "Оценка: склон выше 500 м поднимается над рельефом в секторе окна ±45°, но соседние дома "
        "и деревья не учтены (D18)."
        if seen
        else "Расчёт: в секторе окна ±45° ни один склон выше 500 м не поднимается над рельефом "
        "даже для глаза на верху корпуса и без соседних домов."
    )
    return Fact(
        fact_id=make_fact_id(w.hotel_id, subject, "V02", {}, version),
        hotel_id=w.hotel_id,
        city_id=city_id,
        subject_level="facade",
        subject_id=subject,
        feature_id="V02",
        params={},
        value=value,
        status=status,
        sources=_sources(scene, w.building_id, Cell(None)),
        recipe_id="V02-R1",
        algorithm_version=version,
        confidence=Confidence(level="low" if seen else "medium", reason=reason),
        calculated_at=calculated_at,
        scope_note="Любое окно этого фасада; номер категории — по ответу отеля (D17).",
        recipe_attempts=[RecipeAttempt(recipe_id="V02-R1", data_kind="Б", state="accepted")],
        depends_on=[{"facade_azimuth_deg": round(w.facade["azimuth"], 1), "floors": floors}],
        hard_condition_eligible=not seen,
    )
