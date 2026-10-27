"""Тесты калькуляторов среды и климата (Лера).

Что проверяем:
- сетка ERA5 и правила полноты: пропуск дня отклоняет 30-летнее среднее, а не занижает его;
- границы суточных порогов C02 (`>= 1 мм`) и C03 (`> 30 °C`);
- координатный контракт моря: сдвиг в полшага сетки 1/24°;
- геодезическое расстояние WGS84;
- перенос v9: те же числа на общих отелях (если кеш и факты v9 на диске);
- доля покрова без геобиблиотек (как в образе приложения) — `unknown`, а не падение.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

from calculators.env_climate import (
    YEARS,
    ClimateDay,
    ClimateMonthly,
    climate_aggregate,
    climate_cell,
    wgs84_distance_m,
)
from calculators.env_marine import MarineTemperature, native_cf_coordinate
from core.plugins import CityContext, Subject

BASE = Path("research_notes/Расчёт характеристик")
ERA5 = BASE / "environment" / "raw"
COHORT = BASE / "cohort" / "hotels.jsonl"
V9_FACTS = BASE / "integrated" / "all8_facts.jsonl.gz"


def days_for(month: int, mean: float = 20.0, rain: float = 0.0, maximum: float = 25.0):
    out = []
    for year in YEARS:
        for day in range(1, 32):
            if day > 30 and month in (4, 6, 9, 11):
                continue
            if day > 28 and month == 2 and year % 4:
                continue
            out.append(
                ClimateDay(
                    date=f"{year}-{month:02}-{day:02}",
                    mean=mean,
                    maximum=maximum,
                    rain=rain,
                )
            )
    return out


def test_climate_cell_snaps_to_quarter_degree():
    assert climate_cell(43.61, 39.72) == (43.5, 39.75)
    assert climate_cell(43.5, 39.75) == (43.5, 39.75)


def test_c01_is_mean_of_annual_means():
    value, error = climate_aggregate(days_for(7, mean=20.0), "C01", 7)
    assert error is None
    assert value["mean"] == 20.0
    assert value["n_years"] == 30
    assert value["valid_days"] == 930
    assert set(value["annual_values"]) == {str(y) for y in YEARS}


def test_missing_day_rejects_whole_period():
    days = days_for(7)
    days = [d for d in days if d.date != "2005-07-15"]
    value, error = climate_aggregate(days, "C01", 7)
    assert value is None
    assert error["valid_year_count"] == 29
    assert error["missing_years"][0]["year"] == 2005


def test_none_value_rejects_whole_period():
    days = days_for(7)
    days[0] = ClimateDay(date=days[0].date, mean=None, maximum=25.0, rain=0.0)
    value, error = climate_aggregate(days, "C01", 7)
    assert value is None and error["valid_year_count"] == 29


def test_c02_threshold_is_inclusive():
    value, _ = climate_aggregate(days_for(7, rain=1.0), "C02", 7)
    assert value["mean"] == 31.0
    value, _ = climate_aggregate(days_for(7, rain=0.9), "C02", 7)
    assert value["mean"] == 0.0


def test_c03_threshold_is_strict():
    value, _ = climate_aggregate(days_for(7, maximum=30.0), "C03", 7)
    assert value["mean"] == 0.0
    value, _ = climate_aggregate(days_for(7, maximum=30.1), "C03", 7)
    assert value["mean"] == 31.0


def test_wgs84_degree_on_equator():
    # 1° дуги по экватору на WGS84: a·π/180 = 111 319,49 м
    assert wgs84_distance_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111319.4908, abs=1e-3)


def test_native_cf_coordinate_offsets_half_cell():
    native = native_cf_coordinate(43.625, 39.708344)
    assert native["native_cf_latitude"] == pytest.approx(43.58333206176758)
    assert native["native_cf_longitude"] == pytest.approx(39.66666793823242)
    assert native["api_to_native_offset_deg"] == pytest.approx(1 / 24)


def test_climate_without_cache_is_unknown_with_reason(tmp_path):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=43.6, longitude=39.7
    )
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    facts = ClimateMonthly().run([subject], ctx)
    assert len(facts) == 6
    assert {str(f.status) for f in facts} == {"unknown"}
    assert all(
        f.unknown_reason and f.unknown_reason.startswith("era5_cache_missing") for f in facts
    )
    assert all(f.recipe_attempts for f in facts)


def test_climate_without_coordinates_is_unknown(tmp_path):
    subject = Subject(hotel_id="1", subject_level="hotel", subject_id="hotel:1")
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    facts = ClimateMonthly().run([subject], ctx)
    assert {f.unknown_reason for f in facts} == {"hotel_coordinates_missing"}


def test_marine_is_not_applicable_inland():
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=55.75, longitude=37.6
    )
    ctx = CityContext(city_id="moscow", timezone="Europe/Moscow", data_dir=Path("."))
    facts = MarineTemperature().run([subject], ctx)
    assert len(facts) == 2
    assert {str(f.status) for f in facts} == {"not_applicable"}


def test_marine_without_targets_is_unknown(tmp_path):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=43.6, longitude=39.7
    )
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    facts = MarineTemperature().run([subject], ctx)
    assert {str(f.status) for f in facts} == {"unknown"}
    assert all("marine_targets_missing" in f.unknown_reason for f in facts)


def _cohort(city: str, limit: int) -> list[dict]:
    rows = []
    for line in COHORT.open("r", encoding="utf-8"):
        hotel = json.loads(line)
        if hotel["city_id"] == city:
            rows.append(hotel)
        if len(rows) == limit:
            break
    return rows


@pytest.mark.skipif(not ERA5.is_dir() or not V9_FACTS.is_file(), reason="нет кеша v9 на диске")
@pytest.mark.parametrize("city", ["sochi", "moscow"])
def test_climate_matches_v9_on_common_hotels(city):
    hotels = _cohort(city, 2)
    subjects = [
        Subject(
            hotel_id=h["hotel_id"],
            subject_level="hotel",
            subject_id=f"hotel:{h['hotel_id']}",
            latitude=h["latitude"],
            longitude=h["longitude"],
        )
        for h in hotels
    ]
    ctx = CityContext(city_id=city, timezone="Europe/Moscow", data_dir=ERA5)
    ours = {
        (f.hotel_id, f.feature_id, f.params["month"]): f
        for f in ClimateMonthly().run(subjects, ctx)
    }
    with gzip.open(V9_FACTS, "rt", encoding="utf-8") as f:
        for line in f:
            fact = json.loads(line)
            key = (fact["hotel_id"], fact["feature_id"], fact.get("params", {}).get("month"))
            if key not in ours or fact["city_id"] != city:
                continue
            mine = ours[key]
            assert str(mine.status) == fact["status"]
            assert mine.value["mean"] == pytest.approx(fact["value"]["mean"], abs=1e-9)
            assert mine.value["annual_values"] == fact["value"]["annual_values"]


GEO_LIBS = ("numpy", "rasterio", "shapely", "pyproj")


def test_landcover_without_geo_libs_is_unknown_not_crash(tmp_path, monkeypatch):
    """Ревью №22 и D41 п. 5: в образе нет numpy/rasterio — с тайлом на месте пробный расчёт
    отвечал «калькулятор упал: No module named 'numpy'» вместо честного «неизвестно»."""
    from agents.specgen import trial_run
    from agents.specgen.draft import rules_draft
    from app.stub_store import StubStore
    from calculators.env_landcover import tile_name
    from calculators.env_vegetation import VegetationShare
    from core.reasons import FALLBACK, reason_text

    tiles = tmp_path / "worldcover"
    tiles.mkdir()
    (tiles / tile_name(43.6, 39.7)).write_bytes(b"tile")  # тайл есть, библиотек нет
    store = StubStore.load()
    spec = rules_draft("Доля застройки в 300 м")
    for module in GEO_LIBS:
        monkeypatch.setitem(sys.modules, module, None)

    result = trial_run(spec, store, "sochi", n=5, data_dir=tmp_path)
    assert result["ran"], result.get("reason")
    assert result["unknown_reasons"] == {"geo_libs_missing": result["hotels"]}
    assert result["coverage"] == 0.0

    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=43.6, longitude=39.7
    )
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    (e03,) = VegetationShare().run([subject], ctx)
    assert str(e03.status) == "unknown" and e03.unknown_reason == "geo_libs_missing"
    assert e03.recipe_attempts[-1].reason == "geo_libs_missing: нет numpy"
    assert reason_text("geo_libs_missing") != FALLBACK
