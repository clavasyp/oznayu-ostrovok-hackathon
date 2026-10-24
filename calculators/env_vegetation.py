"""E03 — доля растительности в радиусе 300 м по ESA WorldCover 2021 (пример контракта плагина).

Перенос `green_fraction` из расчётов v9
(research_notes/Расчёт характеристик/scripts/environment.py):
пересечение пикселей с кругом 300 м в азимутальной проекции вокруг отеля; знаменатель — суша
(без воды 80 и пропусков 0). Если покрытие растром < 99% — факт `unknown` с причиной.
Владелец дальше — Лера (calculators/env*).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id
from core.plugins import CityContext, Subject, register_calculator

VEGETATION_CLASSES = (10, 20, 30, 40, 90, 95, 100)
VALID_CLASSES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)
WATER, MISSING = 80, 0
RADIUS_M = 300.0
WORLDCOVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
PARAMS = {
    "radius_m": 300,
    "epoch": 2021,
    "vegetation_classes": list(VEGETATION_CLASSES),
    "denominator": "classified land area excluding permanent water80",
}


def tile_name(lat: float, lon: float) -> str:
    return (
        "ESA_WorldCover_10m_2021_v200_"
        f"N{math.floor(lat / 3) * 3:02}E{math.floor(lon / 3) * 3:03}_Map.tif"
    )


def vegetation_share(lat: float, lon: float, tile: Path) -> tuple[float | None, str | None]:
    """Возвращает (процент растительности на суше, причина неизвестности)."""
    import numpy as np
    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.windows import from_bounds

    projector = Transformer.from_crs(
        "EPSG:4326", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m", always_xy=True
    )
    circle = shapely.Point(0, 0).buffer(RADIUS_M, quad_segs=64)
    with rasterio.open(tile) as ds:
        dx = 330 / (111000 * math.cos(math.radians(lat)))
        dy = 330 / 111000
        win = from_bounds(lon - dx, lat - dy, lon + dx, lat + dy, ds.transform)
        win = win.round_offsets().round_lengths()
        values = ds.read(1, window=win, boundless=True, fill_value=MISSING)
        t = ds.window_transform(win)
    height, width = values.shape
    yy, xx = np.indices((height + 1, width + 1))
    xm, ym = projector.transform(t.c + xx * t.a, t.f + yy * t.e)
    corners = [
        (xm[:-1, :-1], ym[:-1, :-1]),
        (xm[:-1, 1:], ym[:-1, 1:]),
        (xm[1:, 1:], ym[1:, 1:]),
        (xm[1:, :-1], ym[1:, :-1]),
    ]
    vertices = np.stack([np.stack(c, axis=-1) for c in corners], axis=-2).reshape(-1, 4, 2)
    areas = shapely.area(shapely.intersection(shapely.polygons(vertices), circle))
    vals = values.reshape(-1)
    valid = np.isin(vals, VALID_CLASSES)
    land = valid & (vals != WATER)
    if float(areas[valid].sum()) / circle.area < 0.99:
        return None, "worldcover_valid_area_below_99_percent"
    land_area = float(areas[land].sum())
    if land_area <= 0:
        return None, "no_classified_land_in_buffer"
    veg_area = float(areas[land & np.isin(vals, VEGETATION_CLASSES)].sum())
    return 100 * veg_area / land_area, None


@register_calculator
class VegetationShare:
    id = "env.vegetation_share"
    version = "E03-worldcover2021-aeqd@1"
    feature_ids = ("E03",)

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        tiles_dir = Path(ctx.params.get("worldcover_dir", ctx.data_dir / "worldcover"))
        facts = []
        for s in subjects:
            tile = tiles_dir / tile_name(s.latitude, s.longitude)
            attempts = [
                RecipeAttempt(
                    recipe_id="E03-R1",
                    data_kind="Б",
                    state="rejected",
                    reason="Нет более нового лицензированного покрова с общей маской суши.",
                )
            ]
            value, reason = None, None
            if not tile.is_file():
                reason = f"worldcover_tile_missing:{tile.name}"
            else:
                value, reason = vegetation_share(s.latitude, s.longitude, tile)
            attempts.append(
                RecipeAttempt(
                    recipe_id="E03-R2",
                    data_kind="Б",
                    state="accepted" if value is not None else "failed",
                    reason=reason,
                )
            )
            facts.append(
                Fact(
                    fact_id=make_fact_id(s.hotel_id, s.subject_id, "E03", PARAMS, self.version),
                    hotel_id=s.hotel_id,
                    city_id=ctx.city_id,
                    subject_level="hotel",
                    subject_id=s.subject_id,
                    feature_id="E03",
                    params=PARAMS,
                    value=value,
                    unit="%",
                    status=Status.computed if value is not None else Status.unknown,
                    unknown_reason=reason if value is None else None,
                    sources=[
                        Source(
                            provider="ESA WorldCover",
                            url=WORLDCOVER_URL + tile.name,
                            license="CC BY 4.0",
                            dataset_version="WorldCover2021v200",
                        )
                    ],
                    recipe_id="E03-R2",
                    algorithm_version=self.version,
                    confidence=Confidence(
                        level="medium",
                        reason=(
                            "Исторический покров 2021 года, не текущая зелень и не доступный парк."
                        ),
                    ),
                    calculated_at=datetime.now(UTC),
                    recipe_attempts=attempts,
                )
            )
        return facts
