"""Слой «вид и солнце» в пакете города: факты V05 и V01 по окнам (calculators/view_sun.py, D40).

Запуск — у разработчика, с группой builder и материалами расчётов вне репозитория:

    uv run --extra builder python -m pipeline.build_view_sun --city sochi

Пишет `packs/<город>/data/view_sun.jsonl.gz` и добавляет его в манифест пакета (`add_layer`).
Сборка v9 (`build_v9_packs`) такие слои сохраняет.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

from calculators import view_sun
from core.packs import PackSource, add_layer
from pipeline.build_v9_packs import PACKS, ROOT, write_jsonl_gz

NOTES = ROOT.parent / "research_notes"  # материалы расчётов у разработчика (вне репозитория)
V9 = NOTES / "Расчёт характеристик"
AUDIT = NOTES / "Аудит данных 8 городов" / "raw"

CITIES = {
    "sochi": {
        "timezone": "Europe/Moscow",
        "center": (39.7231, 43.5855),  # как CENTERS v9
        "scene": V9 / "cohort" / "osm_sochi_extended.jsonl",
        "matches": V9 / "results" / "facades" / "sochi_building_matches.jsonl",
        "globfp": AUDIT / "globfp_sochi.jsonl",
        "worldcover": AUDIT / "raster_tiles" / "ESA_WorldCover_10m_2021_v200_N42E039_Map.tif",
        "dsm": [
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N43_00_E039_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N43_00_E040_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N44_00_E039_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N44_00_E038_00_DEM.tif",
        ],
        "tile_list": AUDIT / "copdem_tileList.txt",
        # Солнцестояния: крайние азимуты заката за год — для ответа «в любую дату».
        "season": ["2026-12-21", "2027-06-21"],
        # Даты v9 и день выезда: вечера поездок сценариев показа (ноябрь и июль).
        "dates": [
            "2026-11-14",
            "2026-11-15",
            "2026-11-16",
            "2026-11-17",
            "2027-07-15",
            "2027-07-16",
            "2027-07-17",
            "2027-07-18",
        ],  # fmt: skip
    }
}

LAYER = "data/view_sun.jsonl.gz"
CURATED = ROOT / "data" / "curated"  # этажность, проверенная командой по картам (D33)
QUEUE_FIELDS = ["building_id", "levels", "height_m", "source", "source_url", "checked_by",
                "checked_at", "note", "now_known", "cells", "hotels", "osm_url", "yandex_url",
                "twogis_url"]  # fmt: skip
SOURCES = [  # id с префиксом слоя: у разных слоёв источники не пересекаются (add_layer)
    PackSource(
        id="view_sun:osm-buildings",
        provider="OpenStreetMap (сцена v9)",
        license="ODbL-1.0",
        commercial_ok=True,
    ),  # fmt: skip
    PackSource(
        id="view_sun:copdem-glo30",
        provider="Copernicus DEM GLO-30",
        url=view_sun.URLS["copdem"],
        license="Copernicus DEM licence (free, attribution)",
        commercial_ok=True,
    ),
    PackSource(
        id="view_sun:worldcover-2021",
        provider="ESA WorldCover",
        url=view_sun.URLS["worldcover"],
        release="v200 2021",
        license="CC BY 4.0",
        commercial_ok=True,
    ),
    PackSource(
        id="view_sun:3d-globfp-v2",
        provider="3D-GloBFP",
        url=view_sun.URLS["globfp"],
        release="v2 (2025-05-22)",
        license="CC BY 4.0",
        commercial_ok=None,  # права на исходные контуры не выяснены (v4d)
    ),
]


def worldcover_bbox(geoms: list) -> tuple[float, float, float, float]:
    """Окно WorldCover: все корпуса отелей и вокруг них — на дальность моря и ближней зоны."""
    xs, ys = [], []
    for g in geoms:
        x0, y0, x1, y1 = g.bounds
        xs += [x0, x1]
        ys += [y0, y1]
    lat = (min(ys) + max(ys)) / 2
    pad = (view_sun.SEA_MAX_M + view_sun.NEAR_M) / 111_000 + 0.01
    pad_lon = pad / math.cos(math.radians(lat))
    return min(xs) - pad_lon, min(ys) - pad, max(xs) + pad_lon, max(ys) + pad


def inputs_time(cfg: dict, extra: list[Path]) -> datetime:
    """`calculated_at` фактов — время самого свежего входа: тот же вход — тот же пакет (sha256)."""
    paths = [cfg["scene"], cfg["globfp"], cfg["worldcover"], *cfg["dsm"], *extra]
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in paths), UTC).replace(microsecond=0)


def pack_inputs(pack: Path) -> tuple[list[dict], list[dict], list[dict], list[Path]]:
    """Корпуса, фасады и связи «отель → корпус» из пакета города (каталог №26, D41)."""
    from core.packs import iter_jsonl

    paths = [pack / "data" / f"{k}.jsonl.gz" for k in ("buildings", "facades", "hotel_buildings")]
    buildings, facades, links = (list(iter_jsonl(p)) for p in paths)
    return buildings, facades, links, paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="sochi", choices=sorted(CITIES))
    ap.add_argument("--hotels", nargs="*", help="только эти отели (masterId); нужен --out")
    ap.add_argument("--out", type=Path, help="куда писать факты вместо пакета")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="процессов")
    ap.add_argument("--pack", type=Path, help="пакет города с корпусами и фасадами (по умолчанию "
                    "packs/<город>)")  # fmt: skip
    args = ap.parse_args()
    if args.hotels and args.out is None:
        ap.error("--hotels без --out заменил бы слой пакета и очередь проверки частью отелей")
    cfg = CITIES[args.city]

    import shapely

    t0 = time.time()
    pack = args.pack or PACKS / args.city
    buildings, facades, links, inputs = pack_inputs(pack)
    linked = {x["building_id"] for x in links}
    geoms = [shapely.geometry.shape(b["geometry"]) for b in buildings if b["id"] in linked]
    bbox = worldcover_bbox(geoms)  # по всем отелям: часть отелей — то же окно
    if args.hotels:
        links = [x for x in links if str(x["hotel_id"]) in set(args.hotels)]
    curated = CURATED / f"{args.city}_building_heights.csv"
    scene = view_sun.load_scene(
        cfg["scene"], cfg["globfp"], cfg["dsm"], cfg["worldcover"], cfg["tile_list"],
        cfg["center"], bbox, curated,
    )  # fmt: skip
    west, south, east, north = scene.worldcover.bounds[0]
    tol = 1e-3  # градуса: округление окна до пикселей
    if not (west <= bbox[0] + tol and south <= bbox[1] + tol and east >= bbox[2] - tol
            and north >= bbox[3] - tol):  # fmt: skip
        print(f"⚠ WorldCover покрывает {scene.worldcover.bounds[0]}, нужно {bbox}: "
              "за краем — «неизвестно»")  # fmt: skip
    added = view_sun.use_pack_buildings(scene, (b for b in buildings if b["id"] in linked))
    print(f"сцена: {len(scene.osm)} зданий OSM (+{added} из пакета), {len(scene.polys)} "
          f"препятствий, {time.time() - t0:.0f} с")  # fmt: skip
    dates = [date.fromisoformat(d) for d in cfg["dates"]]
    season = tuple(date.fromisoformat(d) for d in cfg["season"])
    calculated_at = inputs_time(cfg, inputs)
    windows = list(view_sun.windows_from_pack(scene, facades, links))
    by_building: dict[str, list] = defaultdict(list)  # корпус — в один процесс: общие кеши
    for w in windows:
        by_building[w.building_id].append(w)
    chunks = sorted(by_building.values(), key=len, reverse=True)
    _STATE["args"] = (scene, dates, season, cfg["timezone"], args.city, calculated_at)
    facts = []
    if args.jobs > 1:
        ctx = multiprocessing.get_context("fork")  # сцена в памяти — общая, не копируется
        with ProcessPoolExecutor(args.jobs, mp_context=ctx) as pool:
            for k, part in enumerate(pool.map(_work, chunks), 1):
                facts += part
                if k % 10 == 0:
                    print(f"  корпусов {k}/{len(chunks)}, фактов {len(facts)}, "
                          f"{time.time() - t0:.0f} с")  # fmt: skip
    else:
        for chunk in chunks:
            facts += _work(chunk)
    rows = sorted(
        facts,
        key=lambda r: (r["hotel_id"], r["feature_id"], r["subject_id"], json.dumps(r["params"]),
                       r["status"]),
    )  # fmt: skip
    stats = Counter(
        (r["feature_id"], r["status"], (r["value"] or {}).get("why") or r["unknown_reason"])
        for r in rows
    )
    for key, n in sorted(stats.items(), key=lambda kv: (kv[0][0], kv[0][1], -kv[1])):
        print(f"  {key}: {n}")
    out = args.out or PACKS / args.city / LAYER
    out.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl_gz(out, rows)
    print(f"{out}: {len(rows)} фактов, {out.stat().st_size / 1e6:.2f} МБ, {time.time() - t0:.0f} с")
    if args.out is None:
        add_layer(pack, LAYER, "facts", len(rows), SOURCES)  # и покрытие V01 / V05 по всем фактам
        write_queue(scene, rows, curated)


_STATE: dict = {}


def _work(chunk: list) -> list[dict]:
    """Факты окон одного корпуса (в отдельном процессе): строки для пакета."""
    scene, dates, season, tz, city, calculated_at = _STATE["args"]
    suns: dict = {}
    rows = []
    for w in chunk:
        for f in view_sun.facts_for_window(scene, w, dates, season, tz, city, calculated_at, suns):
            rows.append(fact_row(f))
    return rows


def fact_row(f) -> dict:
    """Факт как в пакетах v9: пустые поля опускаем, но `value` и `unknown_reason` — всегда."""
    row = f.model_dump(mode="json", exclude_none=True)
    row.setdefault("value", None)
    row.setdefault("unknown_reason", None)
    return row


def write_queue(scene, rows: list[dict], path: Path) -> None:
    """Очередь на проверку этажности: здания, от высоты которых зависит «неизвестно».

    Тот же файл, что читает расчёт: строки, где человек что-то вписал (этажи, источник, кто и
    когда проверил, заметка), сохраняются как есть; остальные — пересчитываются по прогону."""
    import csv

    human = ("levels", "height_m", "source", "source_url", "checked_by", "checked_at", "note")
    filled = {}
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            filled = {r["building_id"]: r for r in csv.DictReader(f)
                      if any(r.get(k) for k in human)}  # fmt: skip
    cells, hotels = Counter(), defaultdict(set)
    for r in rows:
        detail = (r.get("depends_on") or [{}])[0]
        bid = detail.get("decided_by")
        if r["status"] == "unknown" and detail.get("decided_by_kind") in view_sun.BUILDING_KINDS:
            cells[bid] += 1
            hotels[bid].add(r["hotel_id"])
    queue = []
    for bid, n in cells.most_common():
        if bid in filled or bid not in scene.osm:
            continue
        b = scene.osm[bid]
        c = b["geom"].centroid
        lon, lat = scene.inverse.transform(c.x, c.y)
        t = b["tags"]
        now = (
            t.get("height")
            and f"height {t['height']}"
            or t.get("building:levels")
            and (f"levels {t['building:levels']}")
            or b.get("globfp")
            and f"globfp {b['globfp']:.0f} м"
        )
        queue.append({
            "building_id": bid, "now_known": now or "нет", "cells": n,
            "hotels": " ".join(sorted(hotels[bid])), "osm_url": view_sun.URLS["osm"] + bid,
            "yandex_url": f"https://yandex.ru/maps/?ll={lon:.6f},{lat:.6f}&z=19&pt={lon:.6f},{lat:.6f}",
            "twogis_url": f"https://2gis.ru/geo/{lon:.6f}%2C{lat:.6f}",
        })  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, QUEUE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows([*filled.values(), *queue])
    print(f"{path}: проверено {len(filled)}, в очереди {len(queue)}")


if __name__ == "__main__":
    main()
