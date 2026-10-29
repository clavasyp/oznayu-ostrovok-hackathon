"""Слой «вид и солнце» в пакете города: факты V05 и V01 по окнам (calculators/view_sun.py, D40).

Запуск — у разработчика, с группой builder и материалами расчётов вне репозитория:

    uv run --extra builder python -m pipeline.build_view_sun --city sochi

Пишет `packs/<город>/data/view_sun.jsonl.gz` и добавляет его в манифест пакета (`add_layer`).
Сборка v9 (`build_v9_packs`) такие слои сохраняет.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
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
        # Даты v9 и день выезда: вечера поездок сценариев показа (ноябрь и июль); 10–13.11 —
        # заезд по умолчанию в интерфейсе (через две недели от дней показа жюри).
        "dates": [
            "2026-11-10",
            "2026-11-11",
            "2026-11-12",
            "2026-11-13",
            "2026-11-14",
            "2026-11-15",
            "2026-11-16",
            "2026-11-17",
            "2027-07-15",
            "2027-07-16",
            "2027-07-17",
            "2027-07-18",
        ],  # fmt: skip
    },
    # Москва: сцена — здания и деревья из среза Geofabrik ЦФО (`pipeline.build_scene_osm`), высоты
    # без тегов OSM — 3D-GloBFP (`pipeline.extract_globfp`, два тайла figshare). Моря нет: V01 не
    # считаем. За краем листов GLO-30 рельеф не выше 350 м (Смоленско-Московская возвышенность).
    "moscow": {
        "timezone": "Europe/Moscow",
        "center": (37.6176, 55.7558),
        "scene": V9 / "cohort" / "osm_moscow_scene.jsonl",
        "globfp": AUDIT / "globfp_moscow.jsonl",
        "worldcover": AUDIT / "raster_tiles" / "ESA_WorldCover_10m_2021_v200_N54E036_Map.tif",
        "dsm": [
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N55_00_E036_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N55_00_E037_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N56_00_E036_00_DEM.tif",
            AUDIT / "raster_tiles" / "Copernicus_DSM_COG_10_N56_00_E037_00_DEM.tif",
        ],
        "tile_list": AUDIT / "copdem_tileList.txt",
        "region_max_m": 350.0,
        "sea": False,
        # Даты заезда по умолчанию в интерфейсе — через две недели от дней показа жюри (29.10–4.11);
        # июль не считаем: у 2 168 корпусов Москвы каждая дата — около 100 тыс. фактов.
        "season": ["2026-12-21", "2027-06-21"],
        "dates": [
            "2026-11-12",
            "2026-11-13",
            "2026-11-14",
            "2026-11-15",
            "2026-11-16",
            "2026-11-17",
            "2026-11-18",
        ],  # fmt: skip
    },
}

LAYER = "data/view_sun.jsonl.gz"
SUN_LAYER = "data/sun_hours.jsonl.gz"  # часы солнца S01 / S02: верхняя граница по фасаду
MOUNTAIN_LAYER = "data/mountains.jsonl.gz"  # вид на горы V02: «нет» расчётом, «да» оценкой
LANDMARK_LAYER = "data/view_landmarks.jsonl.gz"  # вид на заметный объект V03 (Москва-Сити)
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
# У слоя часов солнца источники свои (id с префиксом слоя): тот же рельеф и контуры OSM.
SUN_SOURCES = [
    PackSource.model_validate(s.model_dump() | {"id": s.id.replace("view_sun:", "sun_hours:")})
    for s in SOURCES
    if not s.id.endswith(("worldcover-2021", "3d-globfp-v2"))
]
MOUNTAIN_SOURCES = [
    PackSource.model_validate(s.model_dump() | {"id": s.id.replace("sun_hours:", "mountains:")})
    for s in SUN_SOURCES
]


def sources_for(cfg: dict) -> list[PackSource]:
    """Источники слоя вида: у Сочи — сцена v9; у Москвы — сцена из среза OSM
    (`pipeline.build_scene_osm`, sha256 и дата среза — в её meta.json). GloBFP — если есть."""
    meta_path = Path(cfg["scene"]).with_suffix(".meta.json")
    if not meta_path.exists():
        return SOURCES
    meta = json.loads(meta_path.read_text("utf-8"))
    scene = PackSource(
        id="view_sun:osm-buildings",
        provider="OpenStreetMap/Geofabrik (здания и деревья вокруг корпусов)",
        release=meta["source_date"],
        sha256=meta["pbf_sha256"],
        license="ODbL-1.0",
        commercial_ok=True,
    )
    rest = [s for s in SOURCES[1:] if cfg.get("globfp") or not s.id.endswith("3d-globfp-v2")]
    return [scene, *rest]


def _renamed(sources: list[PackSource], prefix: str) -> list[PackSource]:
    """Те же источники для слоёв часов солнца и гор: id с префиксом слоя, без WorldCover и
    GloBFP (их эти слои не читают)."""
    return [
        PackSource.model_validate(s.model_dump() | {"id": prefix + s.id.split(":", 1)[1]})
        for s in sources
        if not s.id.endswith(("worldcover-2021", "3d-globfp-v2"))
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


def inputs_time(cfg: dict, pack: Path) -> datetime:
    """`calculated_at` фактов — время самого свежего входа: тот же вход — тот же пакет (sha256).

    Корпуса и фасады — по `built_at` манифеста пакета, а не по времени файлов: `git checkout`
    и перезапись других файлов пакета меняют его, не меняя самих входов."""
    from core.packs import read_manifest

    paths = [p for p in (cfg["scene"], cfg["globfp"], cfg["worldcover"], *cfg["dsm"]) if p]
    files = datetime.fromtimestamp(max(p.stat().st_mtime for p in paths), UTC)
    built = read_manifest(pack).built_at
    return max(files, built).replace(microsecond=0)


def pack_inputs(pack: Path) -> tuple[list[dict], list[dict], list[dict], list[Path]]:
    """Корпуса, фасады и связи «отель → корпус» из пакета города (каталог №26, D41) — все файлы
    вида по манифесту: и сборки каталога, и слои (связи по точке, D42-1)."""
    from core.packs import iter_jsonl, read_manifest

    files = read_manifest(pack).files
    kinds = ("buildings", "facades", "hotel_buildings")
    paths = {k: [pack / f.path for f in files if f.kind == k] for k in kinds}
    buildings, facades, links = ([r for p in paths[k] for r in iter_jsonl(p)] for k in kinds)
    return buildings, facades, links, [p for k in kinds for p in paths[k]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="sochi", choices=sorted(CITIES))
    ap.add_argument("--hotels", nargs="*", help="только эти отели (masterId); нужен --out")
    ap.add_argument("--out", type=Path, help="куда писать факты вместо пакета")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="процессов")
    ap.add_argument("--pack", type=Path, help="пакет города с корпусами и фасадами (по умолчанию "
                    "packs/<город>)")  # fmt: skip
    ap.add_argument("--sun-hours", action="store_true", help="только часы солнца S01 / S02 — "
                    f"отдельный слой {SUN_LAYER}")  # fmt: skip
    ap.add_argument("--mountains", action="store_true", help="только вид на горы V02 — "
                    f"отдельный слой {MOUNTAIN_LAYER}")  # fmt: skip
    ap.add_argument("--landmarks", action="store_true", help="только вид на объекты V03 "
                    f"(view_sun.LANDMARKS) — отдельный слой {LANDMARK_LAYER}")  # fmt: skip
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
        cfg["center"], bbox, curated, cfg.get("region_max_m", view_sun.REGION_MAX_M),
        cfg.get("sea", True),
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
    calculated_at = inputs_time(cfg, pack)
    windows = list(view_sun.windows_from_pack(scene, facades, links))
    by_building: dict[str, list] = defaultdict(list)  # корпус — в один процесс: общие кеши
    for w in windows:
        by_building[w.building_id].append(w)
    chunks = sorted(by_building.values(), key=len, reverse=True)
    _STATE["args"] = (scene, dates, season, cfg["timezone"], args.city, calculated_at)
    work, mode, layer = (
        (_sun_work, "sun", SUN_LAYER) if args.sun_hours
        else (_mountain_work, "mountains", MOUNTAIN_LAYER) if args.mountains
        else (_landmark_work, "landmarks", LANDMARK_LAYER) if args.landmarks
        else (_work, "view", LAYER)
    )  # fmt: skip
    # Сохранение по ходу: готовый корпус сразу дописывается на диск; после обрыва (перезагрузка)
    # повторный запуск с теми же входами пропускает посчитанные корпуса.
    checkpoint = Checkpoint(args.city, mode, {
        "dates": cfg["dates"], "season": cfg["season"], "hotels": sorted(args.hotels or []),
        "versions": [view_sun.VERSION, view_sun.SUN_HOURS_VERSION, view_sun.MOUNTAIN_VERSION]
        + ([view_sun.LANDMARK_VERSION, view_sun.LANDMARKS] if args.landmarks else []),
        "calculated_at": calculated_at.isoformat(), "out": str(args.out or ""),
    })  # fmt: skip
    facts, done = checkpoint.load()
    total = len(chunks)
    chunks = [c for c in chunks if c[0].building_id not in done]
    if done:
        print(f"  продолжаем: готово корпусов {len(done)}, фактов {len(facts)} ({checkpoint.path})")
    if args.jobs > 1:
        ctx = multiprocessing.get_context("fork")  # сцена в памяти — общая, не копируется
        with ProcessPoolExecutor(args.jobs, mp_context=ctx) as pool:
            jobs = {pool.submit(work, chunk): chunk[0].building_id for chunk in chunks}
            for k, job in enumerate(as_completed(jobs), len(done) + 1):
                part = job.result()
                checkpoint.add(jobs[job], part)
                facts += part
                if k % 10 == 0:
                    print(f"  корпусов {k}/{total}, фактов {len(facts)}, "
                          f"{time.time() - t0:.0f} с")  # fmt: skip
    else:
        for chunk in chunks:
            part = work(chunk)
            checkpoint.add(chunk[0].building_id, part)
            facts += part
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
    out = args.out or pack / layer  # пакет `--pack`, а не всегда packs/<город>
    out.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl_gz(out, rows)
    checkpoint.done()
    print(f"{out}: {len(rows)} фактов, {out.stat().st_size / 1e6:.2f} МБ, {time.time() - t0:.0f} с")
    own = sources_for(cfg)
    if args.out is None and args.sun_hours:
        add_layer(pack, SUN_LAYER, "facts", len(rows), _renamed(own, "sun_hours:"))
    elif args.out is None and args.mountains:
        add_layer(pack, MOUNTAIN_LAYER, "facts", len(rows), _renamed(own, "mountains:"))
    elif args.out is None and args.landmarks:
        landmarks = [  # V03 читает и WorldCover, и GloBFP — источники все
            PackSource.model_validate(s.model_dump() | {"id": "landmarks:" + s.id.split(":", 1)[1]})
            for s in own
        ]
        add_layer(pack, LANDMARK_LAYER, "facts", len(rows), landmarks)
    elif args.out is None:
        add_layer(pack, LAYER, "facts", len(rows), own)  # и покрытие V01 / V05 по всем фактам
        write_queue(scene, rows, curated)


CHECKPOINTS = ROOT / ".checkpoints"  # вне пакетов и git: сохранение расчёта по ходу


class Checkpoint:
    """Готовые корпуса расчёта на диске: строка на корпус (`building_id`, факты). Файл — gzip из
    многих частей: каждая дописывается целиком, оборванная последняя часть при чтении
    отбрасывается. Ключ — город, вид расчёта и входы: другие даты или версия — другой файл."""

    def __init__(self, city: str, mode: str, inputs: dict) -> None:
        key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
        self.path = CHECKPOINTS / f"{city}_{mode}_{key}.jsonl.gz"

    def load(self) -> tuple[list[dict], set[str]]:
        facts: list[dict] = []
        done: set[str] = set()
        if not self.path.exists():
            return facts, done
        good: list[str] = []
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as f:
                for line in f:
                    item = json.loads(line)
                    good.append(line)
                    done.add(item["building_id"])
                    facts += item["rows"]
        except (EOFError, OSError, json.JSONDecodeError):
            # Обрыв посреди записи: оставляем только целые корпуса, иначе дописанное после
            # битого хвоста не прочитается; оборванный корпус посчитаем заново.
            with gzip.open(self.path, "wt", encoding="utf-8") as f:
                f.writelines(good)
        return facts, done

    def add(self, building_id: str, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"building_id": building_id, "rows": rows}, ensure_ascii=False)
        with gzip.open(self.path, "at", encoding="utf-8") as f:
            f.write(line + "\n")

    def done(self) -> None:
        self.path.unlink(missing_ok=True)


_STATE: dict = {}


def _landmark_work(chunk: list) -> list[dict]:
    """Вид на объекты V03 фасадов одного корпуса (в отдельном процессе)."""
    scene, _, _, _, city, calculated_at = _STATE["args"]
    return [fact_row(f) for f in view_sun.landmark_facts(scene, chunk, city, calculated_at)]


def _mountain_work(chunk: list) -> list[dict]:
    """Вид на горы V02 фасадов одного корпуса (в отдельном процессе)."""
    scene, _, _, _, city, calculated_at = _STATE["args"]
    return [fact_row(f) for f in view_sun.mountain_facts(scene, chunk, city, calculated_at)]


def _sun_work(chunk: list) -> list[dict]:
    """Часы солнца S01 / S02 фасадов одного корпуса (в отдельном процессе)."""
    scene, _, _, tz, city, calculated_at = _STATE["args"]
    return [fact_row(f) for f in view_sun.sun_hours_facts(scene, chunk, tz, city, calculated_at)]


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
