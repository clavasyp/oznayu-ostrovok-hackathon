"""env.landcover_share — доля классов покрова WorldCover 2021 в радиусе вокруг отеля.

Обобщение E03 (`env.vegetation_share`): там классы растительности и радиус 300 м зашиты, здесь
они приходят параметрами рецепта. Нужен для характеристик, которых нет в каталоге: «доля
застройки в 300 м» (класс 50), «доля воды в 500 м» (80), «доля леса в 1 км» и подобных.
Спецификация такой характеристики собирается в `agents/specgen` и ссылается сюда как
`env.landcover_share@1`.

Параметры (`ctx.params`):

- `classes` — список кодов WorldCover, долю которых считаем, например `[50]` для застройки;
- `radius_m` — радиус круга вокруг отеля, 100–2000 м;
- `worldcover_dir` — где лежат тайлы (по умолчанию `<data_dir>/worldcover`).

Геометрия та же, что в E03: пересечение пикселей с кругом в азимутальной проекции вокруг отеля.
Знаменатель — размеченная суша без воды (класс 80) и пропусков (0), как в E03. Если среди
классов есть вода (80), знаменатель — вся размеченная площадь круга: иначе вода выпала бы и из
числителя, и доля воды всегда была бы 0. Какой знаменатель взят — в `params.denominator`.

Неизвестно, а не заниженная доля: растр покрывает круг меньше чем на 99%; круг выходит за край
растра (вырезка `data/worldcover` — только окно с отелями); нет геобиблиотек (в образе
приложения их нет — `geo_libs_missing`).

Значение — доля в процентах числом, как у E03: движок поиска (`core/search.py`) читает число и
без имени поля, поэтому спецификация может называть поле как угодно.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.facts import Confidence, Fact, RecipeAttempt, Source, Status, make_fact_id
from core.plugins import CityContext, Subject, register_calculator

VALID_CLASSES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)
WATER, MISSING = 80, 0
DEFAULT_RADIUS_M = 300.0
RADIUS_RANGE_M = (100.0, 2000.0)
WORLDCOVER_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
DENOMINATOR_LAND = "classified land area excluding permanent water80"
DENOMINATOR_ALL = "classified area including permanent water80"
GEO_LIBS_MISSING = "geo_libs_missing"


def tile_name(lat: float, lon: float) -> str:
    return (
        "ESA_WorldCover_10m_2021_v200_"
        f"N{math.floor(lat / 3) * 3:02}E{math.floor(lon / 3) * 3:03}_Map.tif"
    )


def counts_water(classes: Sequence[int]) -> bool:
    """Вода среди классов — знаменатель вся размеченная площадь, а не суша."""
    return WATER in classes


def circle_outside(bounds: Any, lat: float, lon: float, radius_m: float) -> bool:
    """Круг выходит за край растра. Вырезка из `data/worldcover` названа как полный тайл ESA,
    а за её краем — пустые пиксели: это «нет тайла», а не «покров размечен меньше 99%».

    Градус здесь — 111 км (чуть меньше настоящего), так что рамка круга берётся с запасом."""
    dx = radius_m / (111000 * math.cos(math.radians(lat)))
    dy = radius_m / 111000
    return (
        lon - dx < bounds.left
        or lon + dx > bounds.right
        or lat - dy < bounds.bottom
        or lat + dy > bounds.top
    )


def landcover_share(
    lat: float, lon: float, tile: Path, classes: Sequence[int], radius_m: float
) -> tuple[float | None, str | None]:
    """Возвращает (доля классов в процентах, причина неизвестности).

    Знаменатель — суша без воды; если среди классов есть вода — вся размеченная площадь."""
    import numpy as np
    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.windows import from_bounds

    projector = Transformer.from_crs(
        "EPSG:4326", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m", always_xy=True
    )
    circle = shapely.Point(0, 0).buffer(radius_m, quad_segs=64)
    with rasterio.open(tile) as ds:
        if circle_outside(ds.bounds, lat, lon, radius_m):
            return None, f"worldcover_tile_missing:{tile.name}:outside_window"
        dx = (radius_m + 30) / (111000 * math.cos(math.radians(lat)))
        dy = (radius_m + 30) / 111000
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
    valid_area = float(areas[valid].sum())
    if valid_area / circle.area < 0.99:
        return None, "worldcover_valid_area_below_99_percent"
    if counts_water(classes):
        # Доля воды: вода — часть знаменателя, иначе она выпала бы и из числителя.
        wanted = valid & np.isin(vals, list(classes))
        return 100 * float(areas[wanted].sum()) / valid_area, None
    land = valid & (vals != WATER)
    land_area = float(areas[land].sum())
    if land_area <= 0:
        return None, "no_classified_land_in_buffer"
    wanted = land & np.isin(vals, list(classes))
    return 100 * float(areas[wanted].sum()) / land_area, None


def _params(classes: Sequence[int], radius_m: float) -> dict[str, Any]:
    # Знаменатель — часть смысла характеристики: он в params, а значит и в fact_id (D15).
    return {
        "classes": list(classes),
        "radius_m": int(radius_m) if float(radius_m).is_integer() else radius_m,
        "epoch": 2021,
        "denominator": DENOMINATOR_ALL if counts_water(classes) else DENOMINATOR_LAND,
    }


@register_calculator
class LandcoverShare:
    """Доля заданных классов WorldCover в заданном радиусе — для спек из specgen."""

    id = "env.landcover_share"
    version = "1"
    # Характеристика задаётся спецификацией, а не каталогом: у калькулятора нет своих feature_id.
    feature_ids: tuple[str, ...] = ()

    def run(self, subjects: Sequence[Subject], ctx: CityContext) -> list[Fact]:
        classes, radius_m, problem = _read_params(ctx)
        tiles_dir = Path(ctx.params.get("worldcover_dir", ctx.data_dir / "worldcover"))
        facts: list[Fact] = []
        for subject in subjects:
            facts.append(self._fact(subject, ctx, tiles_dir, classes, radius_m, problem))
        return facts

    def _fact(
        self,
        subject: Subject,
        ctx: CityContext,
        tiles_dir: Path,
        classes: list[int],
        radius_m: float,
        problem: str | None,
    ) -> Fact:
        params = _params(classes, radius_m)
        attempts = [
            RecipeAttempt(
                recipe_id="landcover-R1",
                data_kind="Б",
                state="rejected",
                reason="Нет более нового лицензированного покрова с общей маской суши.",
            )
        ]
        value: float | None = None
        reason = problem
        detail = None
        sources: list[Source] = []
        if reason is None:
            if subject.latitude is None or subject.longitude is None:
                reason = "hotel_coordinates_missing"
            else:
                tile = tiles_dir / tile_name(subject.latitude, subject.longitude)
                if not tile.is_file():
                    reason = f"worldcover_tile_missing:{tile.name}"
                else:
                    try:
                        value, reason = landcover_share(
                            subject.latitude, subject.longitude, tile, classes, radius_m
                        )
                    except ImportError as e:
                        # Геобиблиотеки — только в группе builder, в образе приложения их нет:
                        # честное «неизвестно», а не «калькулятор упал» в пробном расчёте.
                        reason, detail = GEO_LIBS_MISSING, f"{GEO_LIBS_MISSING}: нет {e.name}"
                    else:
                        sources = [
                            Source(
                                provider="ESA WorldCover",
                                url=WORLDCOVER_URL + tile.name,
                                license="CC BY 4.0",
                                dataset_version="WorldCover2021v200",
                            )
                        ]
        attempts.append(
            RecipeAttempt(
                recipe_id="landcover-R2",
                data_kind="Б",
                state="accepted" if value is not None else "failed",
                reason=detail or reason,
            )
        )
        # ID характеристики из спецификации: иначе факт не связать с черновиком.
        feature_id = str(ctx.params.get("feature_id") or "landcover_share")
        common = {
            "fact_id": make_fact_id(
                subject.hotel_id, subject.subject_id, feature_id, params, self.version
            ),
            "hotel_id": subject.hotel_id,
            "city_id": ctx.city_id,
            "subject_level": "hotel",
            "subject_id": subject.subject_id,
            "feature_id": feature_id,
            "params": params,
            "unit": "%",
            "algorithm_version": self.version,
            "calculated_at": datetime.now(UTC),
            "recipe_attempts": attempts,
        }
        if value is None:
            return Fact(
                **common,
                value=None,
                status=Status.unknown,
                unknown_reason=reason or "landcover_unknown",
                sources=sources,
            )
        return Fact(
            **common,
            value=value,
            status=Status.computed,
            sources=sources,
            recipe_id="landcover-R2",
            confidence=Confidence(
                level="medium",
                reason="Исторический покров 2021 года, не текущее состояние участка.",
            ),
            scope_note=(
                "Доля классов WorldCover 2021 в круге вокруг отеля; не текущая застройка "
                "и не плотность населения."
            ),
            refresh_after="2027-10-24T00:00:00+00:00",
        )


def _read_params(ctx: CityContext) -> tuple[list[int], float, str | None]:
    """Проверяет параметры рецепта. Ошибка — причина «неизвестно», а не исключение."""
    raw = ctx.params.get("classes")
    if not isinstance(raw, list) or not raw:
        return [], DEFAULT_RADIUS_M, "landcover_classes_invalid"
    try:
        classes = [int(code) for code in raw]
    except (TypeError, ValueError):
        return [], DEFAULT_RADIUS_M, "landcover_classes_invalid"
    if any(code not in VALID_CLASSES for code in classes):
        return [], DEFAULT_RADIUS_M, "landcover_classes_invalid"
    try:
        radius_m = float(ctx.params.get("radius_m", DEFAULT_RADIUS_M))
    except (TypeError, ValueError):
        return classes, DEFAULT_RADIUS_M, "landcover_radius_invalid"
    low, high = RADIUS_RANGE_M
    if not low <= radius_m <= high:
        return classes, DEFAULT_RADIUS_M, "landcover_radius_invalid"
    return classes, radius_m, None
