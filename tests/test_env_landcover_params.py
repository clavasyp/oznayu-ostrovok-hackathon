"""`feature_id` черновика из параметров рецепта попадает в факт.

Спецификация из `agents/specgen` приходит с `feature_id` (например `built_up_share_300m`).
Если факт подписан именем калькулятора, движок поиска его не найдёт — условие останется
«неизвестно» при посчитанном значении. Проверяем оба пути: с параметром и без.
"""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rasterio")
pytest.importorskip("shapely")
pytest.importorskip("pyproj")

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

import calculators.env_landcover as landcover  # noqa: E402
from core.plugins import CALCULATORS, CityContext, Subject  # noqa: E402

LAT, LON = 43.5, 39.75


def tile(tmp_path) -> object:
    tiles = tmp_path / "worldcover"
    if (tiles / landcover.tile_name(LAT, LON)).exists():
        return tiles  # второй прогон в том же тесте использует готовый тайл
    tiles.mkdir()
    res, size = 1 / 12000, 1200
    west, north = LON - size / 2 * res, LAT + size / 2 * res
    data = np.full((size, size), 50, dtype="uint8")
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(west, north, res, res),
    }
    with rasterio.open(tiles / landcover.tile_name(LAT, LON), "w", **profile) as ds:
        ds.write(data, 1)
    return tiles


def run(tmp_path, **params):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=LAT, longitude=LON
    )
    ctx = CityContext(
        city_id="sochi",
        timezone="Europe/Moscow",
        data_dir=tmp_path,
        params={"worldcover_dir": tile(tmp_path), "classes": [50], "radius_m": 300, **params},
    )
    (fact,) = CALCULATORS["env.landcover_share"].run([subject], ctx)
    return fact


def test_feature_id_from_params_reaches_fact(tmp_path):
    fact = run(tmp_path, feature_id="built_up_share_300m")
    assert fact.status == "computed"
    assert fact.feature_id == "built_up_share_300m"


def test_feature_id_in_fact_id_so_specs_do_not_collide(tmp_path):
    first = run(tmp_path, feature_id="built_up_share_300m")
    second = run(tmp_path, feature_id="water_share_300m")
    assert first.fact_id != second.fact_id


def test_without_param_falls_back_to_calculator_name(tmp_path):
    fact = run(tmp_path)
    assert fact.feature_id == "landcover_share"
