"""Один отель в готовый пакет города (`pipeline/add_hotel.py`): страница → пакет из одного отеля."""

from datetime import UTC, datetime

import pytest

from core.packs import PackError
from pipeline import add_hotel
from pipeline.build_v9_packs import ROOT

PAGE = ROOT / "data/inputs/ostrovok_pages_2026-10-29"
DISTRICTS = ROOT / "data/inputs/districts_moscow"
URL = "https://ostrovok.ru/hotel/russia/moscow/mid13287330/palmira_art_hotel/"


def test_district_polygon_decides_the_city():
    assert add_hotel.district_of(DISTRICTS, 37.5162, 55.7760)["properties"]["name"] == (
        "Хорошёвский район"
    )
    assert add_hotel.district_of(DISTRICTS, 39.72, 43.58) is None  # Сочи — не Москва


def test_hotel_already_in_city_pack_is_not_added_twice(tmp_path):
    entry = next(PAGE.glob("raw/*/*.html.gz"))
    page = tmp_path / "page.html"
    page.write_bytes(__import__("gzip").decompress(entry.read_bytes()))
    with pytest.raises(PackError, match="уже в пакете"):
        add_hotel.stage(page, URL, datetime(2026, 10, 29, tzinfo=UTC), "moscow", DISTRICTS,
                        tmp_path / "work")  # fmt: skip
    assert "13287330" in add_hotel.city_hotel_ids(ROOT / "packs/moscow")


def test_nearest_stations_follow_the_city_pack_rule():
    rows = add_hotel.station_rows("moscow", {"id": "x", "latitude": 55.776, "longitude": 37.516})
    assert rows and all(r["rank"] <= 3 and r["walking_distance_m"] is None for r in rows)
    by_mode = {}
    for r in rows:
        by_mode.setdefault(r["source"]["mode"], []).append(r["distance_m"])
    assert all(d == sorted(d) for d in by_mode.values())
