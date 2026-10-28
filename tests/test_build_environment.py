"""Слой «среда» — климат и зелень на все отели пакета (pipeline/build_environment.py).

Отели когорты v9 с готовыми фактами не пересчитываются: те же входы дали бы те же `fact_id`,
а загрузка базы держит `fact_id` ключом. Слой попадает в манифест с источниками и покрытием."""

import copy
import gzip
import json
from datetime import UTC, datetime

import pytest

from core.facts import Fact
from core.packs import read_manifest
from pipeline import build_environment
from tests.test_pack_audit import parts as parts  # noqa: F401
from tests.test_pack_audit import write_pack


class FakeCalculator:
    """Вместо ERA5 и WorldCover: факт на каждый переданный отель."""

    def __init__(self, feature_id, ids=None):
        self.feature_id, self.ids, self.seen = feature_id, ids, []

    def run(self, subjects, ctx):
        self.seen += [s.hotel_id for s in subjects]
        return [
            Fact(
                fact_id=(self.ids or {}).get(s.hotel_id, f"{self.feature_id}-{s.hotel_id}"),
                hotel_id=s.hotel_id,
                city_id="moscow",
                subject_level="hotel",
                subject_id=s.subject_id,
                feature_id=self.feature_id,
                value={"mean": 17.5},
                status="computed",
                sources=[{"provider": "test"}],
                observed_at=datetime.now(UTC),
            )  # fmt: skip
            for s in subjects
        ]


def cohort_pack(tmp_path, parts):
    parts = copy.deepcopy(parts)
    parts["hotels"].append({**parts["hotels"][0], "id": "102"})
    parts["facts"] += [
        {"fact_id": f"v9-{fid}", "hotel_id": "101", "city_id": "moscow", "subject_level": "hotel",
         "subject_id": "hotel:101", "feature_id": fid, "status": "computed", "value": {"mean": 1}}
        for fid in ("C01", "C02", "C03")
    ]  # fmt: skip
    return write_pack(tmp_path / "moscow", parts)


def test_cohort_hotels_are_skipped_and_layer_is_in_manifest(tmp_path, parts, monkeypatch):
    pack = cohort_pack(tmp_path, parts)
    climate, green = FakeCalculator("C01"), FakeCalculator("E03")
    monkeypatch.setattr(
        build_environment, "calculators", lambda names: [climate if names == ["climate"] else green]
    )
    rows = build_environment.build("moscow", [], tmp_path, packs=tmp_path)
    assert climate.seen == ["102"]  # у 101 климат уже посчитан в когорте
    assert sorted(green.seen) == ["101", "102"]  # зелени у 101 нет — считаем всем
    assert len(rows) == 3
    entry = {f.path: f for f in read_manifest(pack).files}[build_environment.LAYER]
    assert entry.layer and entry.rows == 3
    layer = [json.loads(line) for line in gzip.open(pack / entry.path, "rt")]
    assert {r["feature_id"] for r in layer} == {"C01", "E03"}


def test_same_fact_id_as_the_pack_is_refused(tmp_path, parts, monkeypatch):
    pack = cohort_pack(tmp_path, parts)
    clash = FakeCalculator("E03", ids={"101": "v9-C01"})
    monkeypatch.setattr(build_environment, "calculators", lambda names: [clash])
    with pytest.raises(SystemExit, match="уже есть в пакете"):
        build_environment.build("moscow", [], tmp_path, packs=tmp_path)
    assert build_environment.LAYER not in {f.path for f in read_manifest(pack).files}


def test_era5_cache_counts_only_successful_answers(tmp_path):
    """Докачка ERA5 смотрит, какие ячейки уже есть: обрезанный ответ (не 200) — не данные."""
    from pipeline.fetch_era5 import cached_cells

    for name, status, cells in (("a", 200, [(43.5, 39.75)]), ("b", 500, [(43.75, 40.0)])):
        body = [{"latitude": lat, "longitude": lon, "daily": {}} for lat, lon in cells]
        (tmp_path / f"era5_sochi_199107_{name}.json").write_text(json.dumps(body))
        meta = {"http_status": status, "params": {"start_date": "1991-07-01"}}
        (tmp_path / f"era5_sochi_199107_{name}.meta.json").write_text(json.dumps(meta))
    assert cached_cells(tmp_path, "sochi") == {(1991, 7): {(43.5, 39.75)}}


def test_climate_sources_are_merged_without_losing_files():
    """30 файлов кеша (по году) — один источник: общие поля как есть, файлы и sha256 списком."""
    from pipeline.build_environment import compact_sources

    sources = [
        {"provider": "Open-Meteo", "license": "CC BY 4.0", "snapshot_file": f"era5_{y}.json",
         "dataset_sha256": f"sha{y}", "start_date": f"{y}-07-01", "json_pointer": "/0",
         "fetched_at": f"2026-10-2{y % 2}T00:00:00+00:00"}
        for y in (1991, 1992)
    ]  # fmt: skip
    (merged,) = compact_sources({"fact_id": "x", "sources": sources})["sources"]
    assert merged["provider"] == "Open-Meteo" and merged["merged_sources"] == 2
    assert merged["snapshot_file_list"] == ["era5_1991.json", "era5_1992.json"]
    assert merged["dataset_sha256_list"] == ["sha1991", "sha1992"]
    assert merged["fetched_at"] == "2026-10-21T00:00:00+00:00"
    one = {"fact_id": "y", "sources": sources[:1]}
    assert compact_sources(one) == one  # один источник не трогаем


def test_osm_start_date_is_a_build_year_estimate_only_when_simple():
    """Год постройки по OSM — оценка: простые записи тега («1912», «~1900») — да, сложные — нет."""
    from pipeline.build_building_years import year_of

    assert year_of({"start_date": "1912"}) == (1912, False, "1912")
    assert year_of({"start_date": "~1900"}) == (1900, True, "~1900")
    assert year_of({"start_date": "1912-05-01"})[0] == 1912
    assert year_of({"start_date": "C19;1910s"}) is None and year_of({}) is None


def test_heritage_objects_keep_only_protected_categories():
    """Реестр наследия из Wikidata: «градоформирующий» и «обладающий признаками» — не охрана;
    номер ЕГРОКН без категории — реестр; у объекта — самая сильная категория."""
    from pipeline.build_heritage import heritage_objects

    def row(q, label, **ids):
        out = {"item": {"value": f"http://www.wikidata.org/entity/{q}"},
               "c": {"value": "Point(37.6 55.75)"}, "itemLabel": {"value": q}}  # fmt: skip
        if label:
            out["designationLabel"] = {"value": label}
        return out | {k: {"value": v} for k, v in ids.items()}

    raw = {"results": {"bindings": [
        row("Q1", "исторически ценный градоформирующий (средоформирующий) объект", kn="1"),
        row("Q2", "выявленный объект культурного наследия России", kn="2"),
        row("Q2", "объект культурного наследия России федерального значения", kn="2"),
        row("Q3", None, egrokn="771"),
        row("Q4", "объект, обладающий признаками объекта культурного наследия", kn="4"),
    ]}}  # fmt: skip
    found = {o["qid"]: o for o in heritage_objects(raw)}
    assert sorted(found) == ["Q2", "Q3"]
    assert found["Q2"]["category"] == "федерального значения"


def test_beach_surface_is_an_estimate_from_the_nearest_beach_tag():
    """B01 — тег `surface` ближайшего пляжа из W01: галька и гравий — `pebble`; без тега или
    с бетонными плитами — факта нет («неизвестно»). Оценка: обязательное условие не закрывает."""
    from pipeline.build_beach_surface import surface_facts

    beaches = {
        "1": {"beach_id": "way/10", "name": "Ставрополье", "distance_m": 810.4},
        "2": {"beach_id": "way/11", "name": None, "distance_m": 300},
        "3": {"beach_id": "way/12", "name": "Бетонный", "distance_m": 90},
    }
    tags = {"way/10": {"surface": "pebblestone"}, "way/11": {}, "way/12": {"surface": "concrete"}}
    meta = {"pbf_sha256": "p" * 64, "source_date": "2026-10-22T20:21:33Z"}
    rows = surface_facts("sochi", beaches, tags, meta, datetime(2026, 10, 28, tzinfo=UTC))
    [row] = rows
    fact = Fact.model_validate(row)
    assert fact.hotel_id == "1" and fact.value["surface"] == "pebble"
    assert fact.status == "estimate" and fact.hard_condition_eligible is False
    assert row["sources"][0]["url"] == "https://www.openstreetmap.org/way/10"
    assert fact.scope_note == "Ближайший пляж «Ставрополье», 810 м пешком: галька по OSM."


def test_construction_distance_is_an_estimate_with_a_lower_bound():
    """X03: расстояние до края ближайшей стройки на карте; ближе 1 км нет — «не ближе 1 км»
    (`bound: lower`), а не «нет стройки». Оценка с коротким сроком свежести."""
    from shapely.geometry import box

    from agents.search import parse_query
    from app.stub_store import StubStore
    from core.search import search
    from pipeline.build_construction import SEARCH_M, construction_facts

    hotels = [
        {"id": "near", "latitude": 55.7500, "longitude": 37.6000},
        {"id": "far", "latitude": 55.8000, "longitude": 37.7000},
    ]
    site = {"id": "way/5", "name": "ЖК", "timestamp": "2026-09-01T00:00:00+00:00",
            "geometry": box(37.6010, 55.7495, 37.6020, 55.7505)}  # fmt: skip
    meta = {"pbf_sha256": "p" * 64, "source_date": "2026-10-22T20:21:33Z"}
    rows = construction_facts("moscow", hotels, [site], meta, datetime(2026, 10, 28, tzinfo=UTC))
    by = {r["hotel_id"]: Fact.model_validate(r) for r in rows}
    assert 55 <= by["near"].value["distance_m"] <= 70 and by["near"].value["site_id"] == "way/5"
    assert by["far"].value == {"distance_m": SEARCH_M, "bound": "lower"}
    assert all(f.status == "estimate" and f.refresh_after for f in by.values())
    store = StubStore(
        hotel_by_id={h: {"hotel_id": h, "city_id": "moscow", "name": h} for h in by},
        by_hotel={h: {"X03": [f]} for h, f in by.items()},
    )
    ast = parse_query("без стройки рядом", "moscow", mode="rules").ast
    said = {r["hotel"]["hotel_id"]: r["soft"][0] for r in search(store, ast)}
    assert {h: (i["verdict"], i["statuses"]) for h, i in said.items()} == {
        "far": ("yes", ["estimate"]),
        "near": ("no", ["estimate"]),
    }


def test_climate_by_points_keys_by_requested_point(tmp_path):
    """Кеш по точкам у отелей (`cell_selection=land`): ключ — запрошенная точка, ячейку ERA5
    выбрал Open-Meteo; две точки в одной ячейке не смешиваются (у каждой своя высота)."""
    from calculators.env_climate import climate_point, load_cache

    assert climate_point(43.4321, 39.9187) == (43.45, 39.9)
    day = {"time": ["2020-07-01"], "temperature_2m_mean": [24.0],
           "temperature_2m_max": [28.0], "precipitation_sum": [0.0]}  # fmt: skip
    payload = [
        {"latitude": 43.5, "longitude": 39.75, "elevation": 9.0, "daily": day},
        {"latitude": 43.5, "longitude": 39.75, "elevation": 300.0,
         "daily": day | {"temperature_2m_mean": [21.0]}},
    ]  # fmt: skip
    (tmp_path / "era5_sochi_202007_x.json").write_text(json.dumps(payload))
    meta = {"http_status": 200, "params": {"latitude": "43.55,43.6", "longitude": "39.7,39.75",
            "cell_selection": "land", "start_date": "2020-07-01"}}  # fmt: skip
    (tmp_path / "era5_sochi_202007_x.meta.json").write_text(json.dumps(meta))
    records, provenance = load_cache(tmp_path, "sochi")
    assert set(records) == {(43.55, 39.7), (43.6, 39.75)}
    assert records[(43.6, 39.75)][0].mean == 21.0
    assert provenance[(43.55, 39.7)][0]["grid_latitude"] == 43.5
