"""Кеш ERA5 (Open-Meteo Historical Weather) для климата C01–C03 на все отели пакета.

python -m pipeline.fetch_era5 --city sochi --cache "<кеш v9>" --out <новый кеш>

Ячейки — сетка 0,25° (`calculators.env_climate.climate_cell`). Уже скачанные ячейки берутся из
`--cache`, недостающие докачиваются в `--out` тем же форматом, что в расчётах v9: запрос на
месяц года, рядом `*.meta.json` с адресом, временем, статусом и sha256 ответа (D22: бесплатный
API — только для разовой сборки пакетов на хакатоне).

С `--points` — точки у отелей с шагом 0,05° (`climate_point`): ячейку ERA5 выбирает Open-Meteo
по рельефу, температура — с поправкой на высоту точки (у побережья Сочи центр ячейки 0,25° —
в горах). Кеш точек — отдельная папка: в нём ключ — запрошенная точка, а не центр ячейки.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx

from calculators.env_climate import (
    MONTHS,
    UTC_OFFSET,
    YEARS,
    _month_length,
    climate_cell,
    climate_point,
)
from core.packs import iter_jsonl
from pipeline.build_v9_packs import PACKS

API = "https://archive-api.open-meteo.com/v1/archive"
DAILY = "temperature_2m_mean,temperature_2m_max,precipitation_sum"
LICENSE = (
    "CC BY 4.0 data; free API non-commercial hackathon D22 only; production Professional/self-host"
)
PAUSE_S = 6.0  # вежливо: каждая точка — вызов API, лимит бесплатного доступа — 600 в минуту
BATCH = 100  # точек в одном запросе: длина адреса


def cached_cells(cache: Path, city: str) -> dict[tuple[int, int], set[tuple[float, float]]]:
    """(год, месяц) → ячейки, уже лежащие в кеше с успешным ответом."""
    out: dict[tuple[int, int], set[tuple[float, float]]] = {}
    for path in sorted(cache.glob(f"era5_{city}_*.json")):
        if path.name.endswith(".meta.json"):
            continue
        meta = path.with_suffix(".meta.json")
        if not meta.exists() or json.loads(meta.read_text("utf-8")).get("http_status") != 200:
            continue
        start = json.loads(meta.read_text("utf-8"))["params"]["start_date"]
        key = (int(start[:4]), int(start[5:7]))
        params = json.loads(meta.read_text("utf-8"))["params"]
        if params.get("cell_selection") == "land":  # запрос по точкам: ключ — запрошенная точка
            lats, lons = str(params["latitude"]).split(","), str(params["longitude"]).split(",")
            out.setdefault(key, set()).update(
                (float(a), float(b)) for a, b in zip(lats, lons, strict=True)
            )
            continue
        payload = json.loads(path.read_text("utf-8"))
        for cell in payload if isinstance(payload, list) else [payload]:
            out.setdefault(key, set()).add((float(cell["latitude"]), float(cell["longitude"])))
    return out


def fetch(
    city: str,
    cells: list[tuple[float, float]],
    year: int,
    month: int,
    out: Path,
    points: bool = False,
) -> int:
    """Один запрос на месяц года для всех недостающих ячеек; ответ и паспорт — рядом."""
    params: dict[str, str] = {
        "latitude": ",".join(str(c[0]) for c in cells),
        "longitude": ",".join(str(c[1]) for c in cells),
        "start_date": f"{year}-{month:02}-01",
        "end_date": f"{year}-{month:02}-{_month_length(year, month):02}",
        "daily": DAILY,
        "models": "era5",
        "timezone": f"Etc/GMT-{UTC_OFFSET[city]}",
        "cell_selection": "nearest",
        "elevation": ",".join("nan" for _ in cells),
    }
    if points:  # ячейку выбирает Open-Meteo по рельефу, температура — на высоту точки
        params["cell_selection"] = "land"
        del params["elevation"]
    url = f"{API}?{urllib.parse.urlencode(params)}"
    requested = datetime.now(UTC)
    for attempt in range(4):
        try:
            response = httpx.get(url, timeout=60)
            response.raise_for_status()
            status, body = response.status_code, response.content
            break
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise
            # 429 — лимит бесплатного API: каждая точка запроса — отдельный вызов, ждём минуту.
            limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
            time.sleep(65 if limited else 5 * (attempt + 1))
    digest = hashlib.sha256(body).hexdigest()
    path = out / f"era5_{city}_{year}{month:02}_{digest[:18]}.json"
    path.write_bytes(body)
    meta = {
        "requested_at": requested.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "url": url,
        "http_status": status,
        "bytes": len(body),
        "sha256": digest,
        "path": path.name,
        "params": params,
        "n_locations": len(cells),
        "license": LICENSE,
    }
    path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, choices=sorted(UTC_OFFSET))
    ap.add_argument("--cache", type=Path, required=True, help="уже скачанный кеш (v9)")
    ap.add_argument("--out", type=Path, required=True, help="куда класть недостающее")
    ap.add_argument("--points", action="store_true", help="точки 0,05° у отелей вместо ячеек")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    hotels = iter_jsonl(PACKS / args.city / "data/hotels.jsonl.gz")
    key = climate_point if args.points else climate_cell
    needed = sorted({key(h["latitude"], h["longitude"]) for h in hotels})
    have = cached_cells(args.cache, args.city)
    for key, cells in cached_cells(args.out, args.city).items():
        have.setdefault(key, set()).update(cells)
    done = 0
    for year in YEARS:
        for month in MONTHS:
            missing = [c for c in needed if c not in have.get((year, month), set())]
            for i in range(0, len(missing), BATCH):
                fetch(args.city, missing[i : i + BATCH], year, month, args.out, args.points)
                done += 1
                time.sleep(PAUSE_S)
    print(f"{args.city}: ячеек {len(needed)}, запросов {done} → {args.out}")


if __name__ == "__main__":
    main()
