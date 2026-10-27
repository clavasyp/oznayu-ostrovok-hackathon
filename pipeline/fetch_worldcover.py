"""Тайлы WorldCover для города — вырезаются из облачных COG, без скачивания 400 МБ.

Зачем: пробный расчёт `agents.specgen` и калькуляторы E03 / `env.landcover_share` ищут тайлы
`ESA_WorldCover_10m_2021_v200_N{широта}E{долгота}_Map.tif` в `<DATA_DIR>/worldcover` или
`WORLDCOVER_DIR`. Целый тайл — 3°×3°, это 36 000×36 000 пикселей и 150–250 МБ на файл; в
репозиторий такое не положить. Поэтому берём из облака только окно с отелями города: имя файла
остаётся каноническим, покрытие — по bbox отелей плюс запас. Это вырезка, а не полный тайл:
калькуляторы проверяют край растра (за ним — «нет тайла»), а в теги GeoTIFF пишется, откуда и
какое окно вырезано (`CROPPED_FROM`, `CROP_BBOX`). В образ приложения вырезки не идут (D41 п. 5).

ESA WorldCover 2021 v200, CC BY 4.0. Источник — открытый бакет ESA, файлы Cloud-Optimized
GeoTIFF, поэтому читаются по сети по диапазонам (GDAL `/vsicurl/`), а не целиком.

Запуск из корня репозитория:

    python -m pipeline.fetch_worldcover --city sochi
    python -m pipeline.fetch_worldcover --city moscow --margin 0.1
    python -m pipeline.fetch_worldcover --city sochi --check     # только посчитать окно
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / "research_notes" / "Расчёт характеристик" / "cohort" / "hotels.jsonl"
BUCKET = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
NAME = "ESA_WorldCover_10m_2021_v200_N{lat:02}E{lon:03}_Map.tif"
TILE_DEG = 3


def tile_name(lat: float, lon: float) -> str:
    lat0 = math.floor(lat / TILE_DEG) * TILE_DEG
    lon0 = math.floor(lon / TILE_DEG) * TILE_DEG
    return NAME.format(lat=lat0, lon=lon0)


def city_bbox(city: str, margin: float) -> tuple[float, float, float, float]:
    """bbox отелей города с запасом: (west, south, east, north)."""
    lats, lons = [], []
    with COHORT.open("r", encoding="utf-8") as f:
        for line in f:
            hotel = json.loads(line)
            if hotel.get("city_id") != city:
                continue
            if hotel.get("latitude") is None or hotel.get("longitude") is None:
                continue
            lats.append(float(hotel["latitude"]))
            lons.append(float(hotel["longitude"]))
    if not lats:
        raise SystemExit(f"нет отелей города {city} в {COHORT}")
    return min(lons) - margin, min(lats) - margin, max(lons) + margin, max(lats) + margin


def tiles_for(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    west, south, east, north = bbox
    lat = math.floor(south / TILE_DEG) * TILE_DEG
    tiles = []
    while lat <= north:
        lon = math.floor(west / TILE_DEG) * TILE_DEG
        while lon <= east:
            tiles.append((lat, lon))
            lon += TILE_DEG
        lat += TILE_DEG
    return tiles


def crop(
    lat: float, lon: float, bbox: tuple[float, float, float, float], out: Path
) -> dict[str, Any]:
    import rasterio
    from rasterio.windows import from_bounds

    name = tile_name(lat, lon)
    url = f"/vsicurl/{BUCKET}/{name}"
    path = out / name
    with rasterio.open(url) as src:
        window = from_bounds(*bbox, src.transform).round_offsets().round_lengths()
        data = src.read(1, window=window)
        profile = src.profile | {
            "height": data.shape[0],
            "width": data.shape[1],
            "transform": src.window_transform(window),
            "compress": "deflate",
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
        }
        out.mkdir(parents=True, exist_ok=True)
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data, 1)
            # CC BY 4.0: изменение указываем — это окно из полного тайла ESA.
            dst.update_tags(
                CROPPED_FROM=f"{BUCKET}/{name}", CROP_BBOX=",".join(f"{v:.6f}" for v in bbox)
            )
    return {
        "file": name,
        "url": f"{BUCKET}/{name}",
        "size_mb": round(path.stat().st_size / 1_048_576, 1),
        "pixels": f"{data.shape[1]}×{data.shape[0]}",
        "bounds": [round(v, 4) for v in bbox],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True)
    parser.add_argument("--margin", type=float, default=0.05, help="запас вокруг отелей, градусы")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "worldcover")
    parser.add_argument("--check", action="store_true", help="только показать окно, не качать")
    args = parser.parse_args()

    bbox = city_bbox(args.city, args.margin)
    tiles = tiles_for(bbox)
    print(f"{args.city}: bbox {tuple(round(v, 3) for v in bbox)}, тайлов {len(tiles)}")
    for lat, lon in tiles:
        print(f"  {tile_name(lat, lon)}")
    if args.check:
        return 0

    total = 0.0
    for lat, lon in tiles:
        info = crop(lat, lon, bbox, args.out)
        total += info["size_mb"]
        print(f"  → {info['file']}: {info['pixels']} px, {info['size_mb']} МБ")
    print(f"\nитого {total:.1f} МБ в {args.out}")
    print(
        f"проверить: WORLDCOVER_DIR={args.out} python -m agents.specgen "
        f'"доля застройки в 300 м" --trial --city {args.city}'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
