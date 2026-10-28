"""Слой «год постройки по OSM» (H01) для корпусов отелей Москвы и Сочи — оценка, не факт.

python -m pipeline.build_building_years

Расчёт v9 год постройки не ставит без документа о вводе здания (`no_verified_construction_
completion_event`): открытие, реконструкция и завершение — разные события. Тег OSM `start_date` —
дата с карты, поэтому здесь это оценка (D18): она поднимает в выдаче отели по «старинному
зданию», но обязательное условие не закрывает. Только простые записи тега: «1912», «~1900»,
«1912-05-01»; «C19;1910s» и подобное пропускаем.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from typing import Any

from core.facts import Fact, make_fact_id
from core.packs import PackSource, add_layer, iter_jsonl, read_manifest
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/building_years.jsonl.gz"
VERSION = "building-year-osm.1"
YEAR = re.compile(r"^(?P<approx>~)?(?P<y>1[0-9]{3}|20[0-2][0-9])(?:-\d{2}(?:-\d{2})?)?$")
SOURCES = [
    PackSource(
        id="building_years:osm",
        provider="OpenStreetMap (теги корпусов пакета)",
        license="ODbL-1.0",
        commercial_ok=True,
    )
]


def year_of(tags: dict[str, Any]) -> tuple[int, bool, str] | None:
    """(год, примерно ли, исходная запись) из `start_date`; сложные записи — None."""
    raw = str(tags.get("start_date") or "").strip()
    m = YEAR.match(raw)
    return (int(m.group("y")), bool(m.group("approx")), raw) if m else None


def rows_of(pack, kind: str):
    for f in read_manifest(pack).files:
        if f.kind == kind:
            yield from iter_jsonl(pack / f.path)


def build(city: str, packs=PACKS) -> list[dict]:
    pack = packs / city
    manifest = read_manifest(pack)
    buildings = {b["id"]: b for b in rows_of(pack, "buildings")}
    out = []
    for link in rows_of(pack, "hotel_buildings"):
        b = buildings.get(link["building_id"])
        found = year_of((b or {}).get("tags") or {})
        if not found:
            continue
        year, approx, raw = found
        hid, bid = str(link["hotel_id"]), link["building_id"]
        fact = Fact(
            fact_id=make_fact_id(hid, bid, "H01", {}, VERSION),
            hotel_id=hid,
            city_id=city,
            subject_level="building",
            subject_id=bid,
            feature_id="H01",
            value={"year": year},
            unit="год",
            status="estimate",
            sources=[
                {
                    "provider": "OpenStreetMap",
                    "url": f"https://www.openstreetmap.org/{bid}",
                    "license": "ODbL-1.0",
                    "source_value": raw,
                }
            ],  # fmt: skip
            recipe_id="H01-OSM-start-date",
            algorithm_version=VERSION,
            confidence={
                "level": "low",
                "reason": "Тег start_date в OSM — дата с карты, не документ о вводе здания"
                + ("; отмечена как примерная" if approx else "")
                + ". Для обязательного условия мало (D18).",
            },
            calculated_at=manifest.built_at,
            scope_note="Корпус, связанный с отелем; год — как отмечен на карте OSM.",
            hard_condition_eligible=False,
        )
        out.append(fact_row(fact))
    rows = sorted(
        {r["fact_id"]: r for r in out}.values(), key=lambda r: (r["hotel_id"], r["fact_id"])
    )
    write_jsonl_gz(pack / LAYER, rows)
    add_layer(pack, LAYER, "facts", len(rows), SOURCES)
    decades = Counter(r["value"]["year"] // 10 * 10 for r in rows)
    print(f"{city}: {len(rows)} фактов у {len({r['hotel_id'] for r in rows})} отелей; "
          f"до 1917 — {sum(n for d, n in decades.items() if d < 1917)}")  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", choices=("moscow", "sochi"), nargs="+", default=["moscow", "sochi"])
    for city in ap.parse_args().city:
        build(city)


if __name__ == "__main__":
    main()
