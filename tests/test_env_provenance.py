"""Паспорта наших фактов не беднее v9: в источнике есть json_pointer, сетка и отметка D22.

Ревью PR #21, п. 4: у C01–C03 терялись `json_pointer`, `grid_*` и `api_entitlement`, у B02 —
`json_pointer`, `fetched_at`, а в параметрах не было `target_source_*`. Проверяем сборщики
паспорта напрямую: им не нужен ни кеш, ни отбор пляжей.
"""

from __future__ import annotations

import calculators.env_climate as climate
import calculators.env_marine as marine


def test_climate_source_keeps_pointer_grid_and_entitlement():
    provenance = [
        {
            "provider": "Open-Meteo Historical Weather API",
            "url": "https://archive-api.open-meteo.com/v1/archive?start_date=1991-07-01",
            "dataset_sha256": "abc",
            "dataset_version": "ERA5 via Open-Meteo API",
            "license": "CC BY 4.0",
            "grid_latitude": 43.5,
            "grid_longitude": 39.75,
            "grid_elevation_m": 0.0,
            "start_date": "1991-07-01",
            "fetched_at": "2026-10-24T02:12:05+00:00",
            "snapshot_file": "era5_sochi_199107_x.json",
            "json_pointer": "/0",
        }
    ]
    (source,) = climate._sources(provenance, 7)
    dumped = source.model_dump(exclude_none=True)
    assert dumped["json_pointer"] == "/0"
    assert dumped["grid_latitude"] == 43.5 and dumped["grid_longitude"] == 39.75
    assert dumped["api_entitlement"].startswith("D22")
    assert dumped["model"] == "ERA5" and dumped["resolution_degrees"] == 0.25


def test_marine_source_keeps_pointer_time_and_entitlement():
    entry = {
        "provider": "Open-Meteo Marine API",
        "url": "https://marine-api.open-meteo.com/v1/marine?start_date=2023-07-01",
        "license": "CC BY 4.0",
        "dataset_version": "meteofrance_currents (MFSST)",
        "dataset_sha256": "def",
        "model_selector": "meteofrance_currents",
        "variable": "thetao",
        "depth_m": 0.494,
        "snapshot_file": "marine_sochi_202307_cells_x.json",
        "json_pointer": "/6",
        "fetched_at": "2026-10-24T02:16:38+00:00",
    }
    dumped = marine._source(entry).model_dump(exclude_none=True)
    assert dumped["json_pointer"] == "/6"
    assert dumped["fetched_at"] == "2026-10-24T02:16:38+00:00"
    assert dumped["api_entitlement"].startswith("D22")


def test_marine_source_takes_provider_from_entry():
    """Пляж приходит из OSM: подписывать его Open-Meteo нельзя (D8, D14)."""
    entry = {"provider": "OpenStreetMap", "license": "ODbL 1.0", "url": "https://osm.org/way/1"}
    dumped = marine._source(entry).model_dump(exclude_none=True)
    assert dumped["provider"] == "OpenStreetMap"
    assert dumped["license"] == "ODbL 1.0"
