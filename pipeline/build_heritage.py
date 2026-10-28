"""Слой «охранный статус здания» (H02) для корпусов отелей Москвы и Сочи.

python -m pipeline.build_heritage --raw <папка с выгрузкой Wikidata>

Источник — объекты Wikidata с номером реестра наследия (P5381 — ЕГРОКН, P1483 —
kulturnoe-nasledie.ru), координатой (P625) и категорией охраны (P1435); CC0. Выгрузка SPARQL —
отдельный шаг, рядом с ней `meta.json` (адрес, время, sha256). Сайт реестра Минкультуры из нашей
сети не отвечает, поэтому реестр — через Wikidata, номер реестра — в паспорте.

Корпус получает «охраняется: да», если точка объекта наследия лежит в его контуре (с запасом
`MARGIN_DEG` на неточность координат). Нет такой точки — факта нет: «неизвестно», а не «нет»
(в Wikidata есть не все объекты). «Исторически ценный градоформирующий объект» и «обладающий
признаками» — не объекты наследия, их не берём.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from core.facts import Fact, make_fact_id
from core.packs import PackSource, add_layer, iter_jsonl, read_manifest, sha256_file
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/heritage.jsonl.gz"
VERSION = "heritage-wikidata.1"
MARGIN_DEG = 0.00008  # ≈ 9 м по широте: точка объекта у фасада, а не строго внутри контура
# Категории охраны (P1435) — от сильной к слабой; остальные — не объект наследия.
CATEGORIES = [
    ("всемирное наследие", "объект всемирного наследия"),
    ("часть объекта всемирного наследия", "часть объекта всемирного наследия"),
    ("объект культурного наследия России федерального значения", "федерального значения"),
    ("объект культурного наследия России регионального значения", "регионального значения"),
    ("памятник архитектуры регионального значения города Москвы", "регионального значения"),
    ("объект культурного наследия России местного (муниципального) значения", "местного значения"),
    ("объект культурного наследия России", "объект культурного наследия"),
    ("выявленный объект культурного наследия России", "выявленный объект"),
]


def heritage_objects(raw: dict) -> list[dict[str, Any]]:
    """Объекты наследия из ответа SPARQL: строка на объект, лучшая категория, номер реестра."""
    rank = {label: i for i, (label, _) in enumerate(CATEGORIES)}
    objects: dict[str, dict[str, Any]] = {}
    for row in raw["results"]["bindings"]:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        point = row["c"]["value"].removeprefix("Point(").removesuffix(")").split()
        o = objects.setdefault(
            qid,
            {"qid": qid, "name": row.get("itemLabel", {}).get("value"), "lon": float(point[0]),
             "lat": float(point[1]), "egrokn": None, "kn": None, "category": None, "rank": 99},
        )  # fmt: skip
        for key in ("egrokn", "kn"):
            o[key] = o[key] or row.get(key, {}).get("value")
        label = row.get("designationLabel", {}).get("value")
        if label in rank and rank[label] < o["rank"]:
            o["rank"], o["category"] = rank[label], dict(CATEGORIES)[label]
    # Номер ЕГРОКН без категории — тоже реестр; одна ссылка kulturnoe-nasledie.ru — нет.
    return [o for o in objects.values() if o["category"] or o["egrokn"]]


def rows_of(pack: Path, kind: str):
    for f in read_manifest(pack).files:
        if f.kind == kind:
            yield from iter_jsonl(pack / f.path)


def build(city: str, raw_dir: Path, packs: Path = PACKS) -> list[dict]:
    from shapely import STRtree
    from shapely.geometry import Point, shape

    raw_path = raw_dir / f"wikidata_{city}.json"
    meta = json.loads((raw_dir / "meta.json").read_text("utf-8"))[city]
    if sha256_file(raw_path) != meta["sha256"]:
        raise SystemExit(f"{city}: выгрузка Wikidata не совпадает с meta.json")
    objects = heritage_objects(json.loads(raw_path.read_text("utf-8")))
    tree = STRtree([Point(o["lon"], o["lat"]) for o in objects])
    pack = packs / city
    manifest = read_manifest(pack)
    buildings = {b["id"]: b for b in rows_of(pack, "buildings")}
    out = []
    for link in rows_of(pack, "hotel_buildings"):
        b = buildings.get(link["building_id"])
        if not b or not b.get("geometry"):
            continue
        area = shape(b["geometry"]).buffer(MARGIN_DEG)
        found = [objects[i] for i in tree.query(area, predicate="intersects")]
        if not found:
            continue
        o = min(found, key=lambda x: x["rank"])
        hid, bid = str(link["hotel_id"]), link["building_id"]
        registry = o["egrokn"] or o["kn"]
        out.append(
            fact_row(
                Fact(
                    fact_id=make_fact_id(hid, bid, "H02", {}, VERSION),
                    hotel_id=hid,
                    city_id=city,
                    subject_level="building",
                    subject_id=bid,
                    feature_id="H02",
                    value={
                        "heritage": True,
                        "category": o["category"],
                        "registry_id": registry,
                        "name": o["name"],
                    },  # fmt: skip
                    status="observed",
                    sources=[
                        {
                            "provider": "Wikidata (реестр объектов наследия)",
                            "license": "CC0 1.0",
                            "url": f"https://www.wikidata.org/wiki/{o['qid']}",
                            "dataset_sha256": meta["sha256"],
                            "fetched_at": meta["fetched_at"],
                            "registry_id": registry,
                        }
                    ],  # fmt: skip
                    recipe_id="H02-WIKIDATA-POINT",
                    algorithm_version=VERSION,
                    confidence={
                        "level": "medium",
                        "reason": "Точка объекта наследия из Wikidata в контуре корпуса отеля; "
                        "границы территории объекта не проверены.",
                    },
                    calculated_at=manifest.built_at,
                    scope_note=f"«{o['name']}», {o['category'] or 'в реестре ЕГРОКН'}.",
                )
            )
        )
    rows = sorted(
        {r["fact_id"]: r for r in out}.values(), key=lambda r: (r["hotel_id"], r["fact_id"])
    )
    write_jsonl_gz(pack / LAYER, rows)
    source = PackSource(id="heritage:wikidata", provider="Wikidata", license="CC0 1.0",
                        sha256=meta["sha256"], commercial_ok=True)  # fmt: skip
    add_layer(pack, LAYER, "facts", len(rows), [source])
    cats = Counter(r["value"]["category"] for r in rows)
    print(f"{city}: объектов наследия {len(objects)}; корпусов отелей под охраной {len(rows)} "
          f"у {len({r['hotel_id'] for r in rows})} отелей; {dict(cats)}")  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--raw", type=Path, required=True, help="папка с wikidata_<город>.json и meta.json"
    )
    ap.add_argument("--city", choices=("moscow", "sochi"), nargs="+", default=["moscow", "sochi"])
    args = ap.parse_args()
    for city in args.city:
        build(city, args.raw)


if __name__ == "__main__":
    main()
