"""Принадлежность города — полигон; подзона — именованный полигон OSM, без догадки по URL.

В пакете у отеля Сочи `subzone` — русское название для гостя («Адлер»), `subzone_id` — код.
"""

from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Point, shape

from core.packs import sha256_file

SUBZONES = {
    "сириус": "sirius",
    "городской округ сириус": "sirius",
    "адлер": "adler",
    "красная поляна": "krasnaya_polyana",
    "эстосадок": "krasnaya_polyana",
    "эсто-садок": "krasnaya_polyana",
    "курорт красная поляна": "krasnaya_polyana",
    "курорт красная поляна 960": "krasnaya_polyana",
    "роза хутор": "krasnaya_polyana",
    "лазаревское": "lazarevskoe",
    "лоо": "loo",
    "дагомыс": "dagomys",
    "хоста": "khosta",
    "центральный внутригородской район": "center",
}
OTHER_SUBZONE = "other_greater_sochi"
SUBZONE_NAMES = {
    "center": "Сочи (центр)",
    "adler": "Адлер",
    "sirius": "Сириус",
    "krasnaya_polyana": "Красная Поляна",
    "lazarevskoe": "Лазаревское",
    "loo": "Лоо",
    "dagomys": "Дагомыс",
    "khosta": "Хоста",
    OTHER_SUBZONE: "Большой Сочи",  # вне именованных подзон
}


class CityGeography:
    def __init__(self, osm_root: Path):
        self.cities = {}
        self.sources = {}
        self.zones = []
        for city in ("moscow", "sochi"):
            path = osm_root / city / "boundary.geojson"
            doc = json.loads(path.read_text("utf-8"))
            geom = shape(doc["union_geometry"])
            if (
                geom.is_empty
                or not geom.is_valid
                or geom.geom_type not in {"Polygon", "MultiPolygon"}
            ):
                raise ValueError(f"{city}: непригодный полигон границы")
            self.cities[city] = geom
            self.sources[city] = {
                "boundary_sha256": sha256_file(path),
                "osm_source_sha256": doc["source_sha256"],
                "source_date": doc["source_date"],
                "relations": [f["properties"]["osm_id"] for f in doc["features"]],
            }
        localities = json.loads((osm_root / "sochi/localities.geojson").read_text("utf-8"))
        for feature in localities["features"]:
            name = feature["properties"].get("name", "").strip().casefold()
            geom = shape(feature["geometry"])
            if name in SUBZONES and geom.geom_type in {"Polygon", "MultiPolygon"}:
                self.zones.append((SUBZONES[name], geom, name))
        # Сириус имеет приоритет над перекрывающимся именованным полигоном Адлера.
        self.zones.sort(key=lambda z: (z[0] != "sirius", z[1].area, z[2]))

    def city_for(self, longitude: float, latitude: float) -> str | None:
        point = Point(longitude, latitude)
        matches = [city for city, geom in self.cities.items() if geom.covers(point)]
        if len(matches) > 1:
            raise ValueError("Точка попала в несколько городов; проверьте границы")
        return matches[0] if matches else None

    def subzone_for(self, longitude: float, latitude: float) -> tuple[str, str | None]:
        point = Point(longitude, latitude)
        for zone, geom, name in self.zones:
            if geom.covers(point):
                return zone, name
        return OTHER_SUBZONE, None
