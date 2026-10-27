"""Тесты калькулятора `env.landcover_share` — универсальной доли классов WorldCover.

Калькулятор нужен характеристикам, которых нет в каталоге: спецификация приходит из
`agents/specgen` и ссылается сюда с параметрами `classes` и `radius_m`. Проверяем: доля
считается на синтетическом растре; знаменатель — суша, а для воды (80) — вся размеченная
площадь; плохие параметры, отсутствие тайла и круг за краем вырезки дают `unknown` с причиной,
а не исключение и не заниженную долю.
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


def synthetic_tiles(tmp_path, *, west_class: int = 10, east_class: int = 50, east_px: int = 600):
    """Тайл WorldCover: западная половина — один класс, восточная — другой.

    `east_px` — сколько пикселей растра восточнее отеля: меньше 600 — вырезка, край которой
    ближе к отелю, чем у полного тайла."""
    tiles = tmp_path / "worldcover"
    tiles.mkdir()
    res = 1 / 12000  # ~10 м
    size = 1200
    west, north = LON - size / 2 * res, LAT + size / 2 * res
    data = np.full((size, size // 2 + east_px), east_class, dtype="uint8")
    data[:, : size // 2] = west_class
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": data.shape[1],
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(west, north, res, res),
    }
    with rasterio.open(tiles / landcover.tile_name(LAT, LON), "w", **profile) as ds:
        ds.write(data, 1)
    return tiles


def run(tmp_path, tiles, **params):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=LAT, longitude=LON
    )
    ctx = CityContext(
        city_id="sochi",
        timezone="Europe/Moscow",
        data_dir=tmp_path,
        params={"worldcover_dir": tiles, **params},
    )
    (fact,) = CALCULATORS["env.landcover_share"].run([subject], ctx)
    return fact


def test_built_up_share_on_synthetic_tile(tmp_path):
    tiles = synthetic_tiles(tmp_path)
    fact = run(tmp_path, tiles, classes=[50], radius_m=300)
    assert fact.status == "computed"
    assert 45 < fact.value < 55  # восточная половина круга — застройка
    assert fact.unit == "%"
    assert fact.params["classes"] == [50] and fact.params["radius_m"] == 300
    assert [a.recipe_id for a in fact.recipe_attempts] == ["landcover-R1", "landcover-R2"]


def test_several_classes_cover_whole_land(tmp_path):
    tiles = synthetic_tiles(tmp_path)
    fact = run(tmp_path, tiles, classes=[10, 50], radius_m=300)
    assert fact.status == "computed"
    assert fact.value == pytest.approx(100.0, abs=0.5)


def test_radius_changes_window_but_not_share(tmp_path):
    tiles = synthetic_tiles(tmp_path)
    small = run(tmp_path, tiles, classes=[50], radius_m=150)
    big = run(tmp_path, tiles, classes=[50], radius_m=1000)
    assert small.status == big.status == "computed"
    assert small.value == pytest.approx(big.value, abs=1.0)
    assert small.params["radius_m"] == 150 and big.params["radius_m"] == 1000


def test_water_is_not_counted_as_land(tmp_path):
    # восточная половина — вода: суши остаётся западная, застройки на ней нет
    tiles = synthetic_tiles(tmp_path, east_class=80)
    fact = run(tmp_path, tiles, classes=[50], radius_m=300)
    assert fact.status == "computed"
    assert fact.value == pytest.approx(0.0, abs=0.5)
    assert fact.params["denominator"] == landcover.DENOMINATOR_LAND


def test_water_share_is_counted_against_whole_classified_area(tmp_path):
    """Ревью №22: «доля воды в 500 м» была 0% со статусом computed у отелей на первой линии —
    вода выпадала и из числителя, и из знаменателя."""
    tiles = synthetic_tiles(tmp_path, west_class=50, east_class=80)
    water = run(tmp_path, tiles, classes=[80], radius_m=300)
    assert water.status == "computed"
    assert water.value == pytest.approx(50.0, abs=1.0)  # восточная половина круга — вода
    assert water.params["denominator"] == landcover.DENOMINATOR_ALL
    # Застройка вместе с водой — вся размеченная площадь, вода не теряется
    both = run(tmp_path, tiles, classes=[50, 80], radius_m=300)
    assert both.value == pytest.approx(100.0, abs=0.5)
    # Без воды знаменатель — суша: вся суша здесь застроена
    built = run(tmp_path, tiles, classes=[50], radius_m=300)
    assert built.value == pytest.approx(100.0, abs=0.5)
    assert built.params["denominator"] == landcover.DENOMINATOR_LAND
    # Разный знаменатель — разный смысл, значит и разные факты (D15)
    assert len({water.fact_id, both.fact_id, built.fact_id}) == 3


def test_circle_beyond_cropped_tile_is_missing_tile_not_low_coverage(tmp_path):
    """Вырезка названа как полный тайл ESA: за её краем — не «размечено меньше 99%», а «нет
    тайла» (ревью №22: отели за окном вырезки)."""
    tiles = synthetic_tiles(tmp_path, east_px=10)  # край растра в ~70 м восточнее отеля
    fact = run(tmp_path, tiles, classes=[50], radius_m=300)
    assert fact.status == "unknown"
    assert fact.unknown_reason.startswith("worldcover_tile_missing:")
    assert fact.unknown_reason.endswith(":outside_window")


def test_e03_beyond_cropped_tile_is_missing_tile(tmp_path):
    from calculators.env_vegetation import VegetationShare

    tiles = synthetic_tiles(tmp_path, east_px=10)
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=LAT, longitude=LON
    )
    ctx = CityContext(
        city_id="sochi",
        timezone="Europe/Moscow",
        data_dir=tmp_path,
        params={"worldcover_dir": tiles},
    )
    (fact,) = VegetationShare().run([subject], ctx)
    assert fact.status == "unknown"
    assert fact.unknown_reason.endswith(":outside_window")


@pytest.mark.parametrize(
    "params, reason",
    [
        ({"classes": [], "radius_m": 300}, "landcover_classes_invalid"),
        ({"classes": [999], "radius_m": 300}, "landcover_classes_invalid"),
        ({"classes": "50", "radius_m": 300}, "landcover_classes_invalid"),
        ({"classes": [50], "radius_m": 50}, "landcover_radius_invalid"),
        ({"classes": [50], "radius_m": 5000}, "landcover_radius_invalid"),
    ],
)
def test_bad_params_are_unknown_not_crash(tmp_path, params, reason):
    tiles = synthetic_tiles(tmp_path)
    fact = run(tmp_path, tiles, **params)
    assert fact.status == "unknown"
    assert fact.unknown_reason == reason


def test_missing_tile_is_unknown_with_reason(tmp_path):
    fact = run(tmp_path, tmp_path / "none", classes=[50], radius_m=300)
    assert fact.status == "unknown"
    assert fact.unknown_reason.startswith("worldcover_tile_missing")


def test_missing_coordinates_are_unknown(tmp_path):
    subject = Subject(hotel_id="1", subject_level="hotel", subject_id="hotel:1")
    ctx = CityContext(
        city_id="sochi",
        timezone="Europe/Moscow",
        data_dir=tmp_path,
        params={"worldcover_dir": tmp_path, "classes": [50], "radius_m": 300},
    )
    (fact,) = CALCULATORS["env.landcover_share"].run([subject], ctx)
    assert fact.unknown_reason == "hotel_coordinates_missing"
