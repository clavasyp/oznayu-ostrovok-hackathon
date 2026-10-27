"""Прогон калькуляторов среды (`calculators/env_run.py`) и паспорта его фактов — по ревью №22.

Что проверяем:
- кеш ERA5 находится при любой раскладке `--data-dir`: плоской, `<dir>/era5` и v9
  `<dir>/environment/raw` (раньше все 534 факта климата получали `era5_cache_missing`);
- защита «E03 пропущен» действительно убирает E03 из прогона, когда нет геобиблиотек;
- в `fact_id` и в коде причины нет путей с машины разработчика: тот же отбор пляжей из другой
  папки даёт тот же ID, а `move_provenance_params` не падает;
- паспорт B02 не беднее v9: след замены морской ячейки, источник OSM целиком и без отметки D22.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import calculators.env_run as env_run
from calculators.env_climate import ClimateMonthly
from calculators.env_marine import MarineTemperature, worldcover_class
from core.facts import make_fact_id
from core.packs import PROVENANCE_PARAMS, move_provenance_params
from core.plugins import CityContext, Subject

V9 = Path("research_notes/Расчёт характеристик")
RAW = V9 / "environment" / "raw"
COHORT = V9 / "cohort" / "hotels.jsonl"


def args_for(data_dir: Path, **extra) -> argparse.Namespace:
    values = {
        "data_dir": data_dir,
        "era5_dir": None,
        "marine_targets": None,
        "marine_mapping": None,
        "worldcover_dir": None,
    }
    return argparse.Namespace(**(values | extra))


@pytest.mark.parametrize("layout", ["", "era5", "environment/raw"])
def test_era5_cache_found_in_every_layout(tmp_path, layout):
    folder = tmp_path / layout
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "era5_sochi_199107_x.json").write_text("[]", "utf-8")
    ctx = env_run.build_context(args_for(tmp_path), "sochi")
    assert Path(ctx.params["era5_dir"]) == folder


def test_explicit_era5_dir_wins_and_missing_cache_is_left_to_calculator(tmp_path):
    ctx = env_run.build_context(args_for(tmp_path, era5_dir=tmp_path / "x"), "sochi")
    assert ctx.params["era5_dir"] == tmp_path / "x"
    # Кеша нигде нет — ключа нет, калькулятор ищет сам и отвечает «нет кеша».
    assert "era5_dir" not in env_run.build_context(args_for(tmp_path), "sochi").params


def _sochi_subjects(limit: int = 2) -> list[Subject]:
    subjects = []
    for line in COHORT.open(encoding="utf-8"):
        hotel = json.loads(line)
        if hotel["city_id"] != "sochi":
            continue
        subjects.append(
            Subject(
                hotel_id=hotel["hotel_id"],
                subject_level="hotel",
                subject_id=f"hotel:{hotel['hotel_id']}",
                latitude=hotel["latitude"],
                longitude=hotel["longitude"],
            )
        )
        if len(subjects) == limit:
            break
    return subjects


@pytest.mark.skipif(not RAW.is_dir() or not COHORT.is_file(), reason="нет кеша ERA5 v9 на диске")
def test_climate_is_computed_with_v9_and_nested_data_dir(tmp_path):
    """Сценарий ревью: `--data-dir` с раскладкой v9 и `<dir>/era5` давали 534 «неизвестно»."""
    (tmp_path / "era5").symlink_to(RAW.resolve(), target_is_directory=True)
    subjects = _sochi_subjects()
    for data_dir in (V9, tmp_path):
        ctx = env_run.build_context(args_for(data_dir), "sochi")
        facts = ClimateMonthly().run(subjects, ctx)
        assert {str(f.status) for f in facts} == {"computed"}, data_dir


def test_e03_guard_really_skips_vegetation(monkeypatch):
    real = env_run.importlib.util.find_spec
    monkeypatch.setattr(
        env_run.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "numpy" else real(name, *a),
    )
    assert env_run.calculators(["vegetation"]) == []
    assert [c.id for c in env_run.calculators(["climate", "vegetation"])] == ["env.climate_monthly"]


def _no_local_path(fact) -> dict:
    stored = move_provenance_params(fact.model_dump(mode="json"))  # PackError — провал теста
    assert str(Path.home()) not in json.dumps(stored, ensure_ascii=False)
    return stored


def test_missing_inputs_give_reason_codes_without_paths(tmp_path):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=43.6, longitude=39.7
    )
    ctx = CityContext(city_id="sochi", timezone="Europe/Moscow", data_dir=tmp_path)
    climate = ClimateMonthly().run([subject], ctx)
    marine = MarineTemperature().run([subject], ctx)
    assert {f.unknown_reason for f in climate} == {"era5_cache_missing"}
    assert {f.unknown_reason for f in marine} == {"marine_targets_missing"}
    for fact in climate + marine:
        _no_local_path(fact)
    # Где искали — в попытке рецепта, путём от папки проекта, а не с машины разработчика.
    assert climate[0].recipe_attempts[-1].reason.startswith("era5_cache_missing: ")


# --- B02 на синтетическом кеше: одна морская ячейка, июль 2023–2025 -----------------------

CELL = (43.541664, 39.875015)
NATIVE = (43.5, 39.83333206176758)
OSM_SOURCE = {
    "provider": "OpenStreetMap",
    "url": "https://www.openstreetmap.org/relation/12429089",
    "license": "ODbL1.0",
    "license_url": "https://www.openstreetmap.org/copyright",
    "dataset_version": "2026-10-22T20:21:33Z",
    "date_kind": "OSM edit, not physical inspection",
    "observed_at": "2026-06-16 10:55:41+00:00",
    "version": 2,
}


def marine_inputs(root: Path, *, rejected: bool = False) -> tuple[Path, Path]:
    """Кеш Open-Meteo Marine + отбор «отель → пляж» + карта ячеек, как у Севы и в v9."""
    cache = root / "marine" / "raw"
    cache.mkdir(parents=True)
    for year in (2023, 2024, 2025):
        stamps = [f"{year}-07-{d:02}T{h:02}:00" for d in range(1, 32) for h in (0, 6, 12, 18)]
        payload = {
            "latitude": CELL[0],
            "longitude": CELL[1],
            "hourly": {"time": stamps, "sea_surface_temperature": [24.5] * len(stamps)},
        }
        name = f"marine_sochi_{year}07_cells_x"
        (cache / f"{name}.json").write_text(json.dumps(payload), "utf-8")
        meta = {
            "http_status": 200,
            "url": "https://marine-api.open-meteo.com/v1/marine?x",
            "sha256": "s",
        }
        (cache / f"{name}.meta.json").write_text(json.dumps(meta), "utf-8")
    targets = root / "targets.jsonl"
    target = {
        "hotel_id": "1",
        "beach_id": "relation/12429089",
        "beach_kind": "sea",
        "marine_classification": {"accepted": True},
    }
    targets.write_text(json.dumps(target) + "\n", "utf-8")
    beach = {
        "eligible": True,
        "model_latitude": CELL[0],
        "model_longitude": CELL[1],
        "native_cf_latitude": NATIVE[0],
        "native_cf_longitude": NATIVE[1],
        "native_coordinate_worldcover2021_class": 80,
        "beach_to_cell_m": 3806.4,
        "source": OSM_SOURCE,
    }
    if rejected:
        beach["wet_cell_selection_method"] = "nearest candidate native water centre within30km"
        beach["rejected_initial_cell"] = {
            "model_latitude": 43.4,
            "native_coordinate_worldcover2021_class": 50,
            "probe_source": {"path": "/Users/someone/raw/probe.json"},
            "reason": "Finite model wetmask conflicts with classified land.",
        }
    mapping = root / "mapping.json"
    mapping.write_text(json.dumps({"beaches": {"relation/12429089": beach}}), "utf-8")
    return targets, mapping


def marine_july(root: Path, targets: Path, mapping: Path):
    subject = Subject(
        hotel_id="1", subject_level="hotel", subject_id="hotel:1", latitude=43.5, longitude=39.86
    )
    ctx = CityContext(
        city_id="sochi",
        timezone="Europe/Moscow",
        data_dir=root,
        params={"marine_targets": targets, "marine_mapping": mapping},
    )
    return next(f for f in MarineTemperature().run([subject], ctx) if f.params["month"] == 7)


def test_marine_fact_id_does_not_depend_on_folder(tmp_path):
    """Ревью №22: тот же отбор из другой папки давал 0 общих fact_id из 178."""
    first = marine_july(tmp_path / "a", *marine_inputs(tmp_path / "a"))
    second = marine_july(tmp_path / "b", *marine_inputs(tmp_path / "b"))
    assert str(first.status) == "estimate" and first.value["mean_C"] == pytest.approx(24.5)
    assert first.fact_id == second.fact_id
    assert first.params["target_source_path"] == "targets.jsonl"  # без пути с машины
    stored = _no_local_path(first)
    assert "target_source_path" not in stored["params"]
    # ID пересчитывается из сохранённого факта — после переноса путей в evidence_files
    assert not set(PROVENANCE_PARAMS) & set(stored["params"])
    again = make_fact_id("1", "hotel:1", "B02", stored["params"], first.algorithm_version)
    assert again == first.fact_id


def test_marine_osm_source_is_whole_and_without_open_meteo_terms(tmp_path):
    fact = marine_july(tmp_path, *marine_inputs(tmp_path))
    osm = [s.model_dump(exclude_none=True) for s in fact.sources if s.provider == "OpenStreetMap"]
    assert len(osm) == 1
    assert "api_entitlement" not in osm[0]  # D22 — про API Open-Meteo, не про OSM
    for key in ("version", "observed_at", "license_url", "date_kind"):
        assert osm[0][key] == OSM_SOURCE[key]
    meteo = [s.model_dump(exclude_none=True) for s in fact.sources if s.provider != "OpenStreetMap"]
    assert meteo and all(s["api_entitlement"].startswith("D22") for s in meteo)


def test_marine_keeps_trace_of_replaced_wet_cell(tmp_path):
    """У 84 фактов Дубая и Стамбула в v9 есть след замены сухой ячейки — у нас его не было."""
    fact = marine_july(tmp_path, *marine_inputs(tmp_path, rejected=True))
    assert fact.params["wet_cell_selection_method"].startswith("nearest candidate")
    rejection = fact.params["initial_wet_cell_rejection"]
    assert rejection["native_coordinate_worldcover2021_class"] == 50
    assert "probe_source" not in rejection  # там путь с машины разработчика, как в v9 — убран
    plain = marine_july(tmp_path / "plain", *marine_inputs(tmp_path / "plain"))
    assert "initial_wet_cell_rejection" not in plain.params


def test_marine_without_own_tile_takes_class_from_mapping(tmp_path):
    fact = marine_july(tmp_path, *marine_inputs(tmp_path))
    assert fact.params["native_cf_worldcover2021_class"] == 80
    assert "по карте ячеек" in fact.params["wet_cell_evidence"]


def test_worldcover_point_outside_crop_or_nodata_is_none(tmp_path):
    """Вырезка названа как полный тайл: за её краем `sample` даёт 0 — это «нет данных»,
    а не класс (ревью №22: у 34 фактов моря класс 80 становился 0)."""
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    tiles = tmp_path / "worldcover"
    tiles.mkdir()
    data = np.full((100, 100), 80, dtype="uint8")
    data[:10, :10] = 0  # пиксели без данных в углу
    profile = {
        "driver": "GTiff",
        "height": 100,
        "width": 100,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(39.80, 43.60, 0.001, 0.001),  # окно 39,80–39,90 × 43,50–43,60
        "nodata": 0,
    }
    name = "ESA_WorldCover_10m_2021_v200_N42E039_Map.tif"
    with rasterio.open(tiles / name, "w", **profile) as ds:
        ds.write(data, 1)
    assert worldcover_class(tiles, 43.55, 39.85) == 80
    assert worldcover_class(tiles, 43.40, 39.85) is None  # южнее окна, тайл «тот же»
    assert worldcover_class(tiles, 43.595, 39.805) is None  # пиксель без данных
