"""Слой «удобства отеля» со страниц Островка: лифт, парковка, круглосуточная стойка, зарядка и
«в отеле есть» (бассейн, спа, фитнес, ресторан…) — заявление отеля о себе (D26).

python -m pipeline.build_hotel_amenities --snapshot ../data/ostrovok_catalog/snapshot_2026-10-24

Пишет `packs/<город>/data/hotel_amenities.jsonl.gz` для отелей пакета и добавляет его в манифест
(`add_layer`); сборка каталога слой сохраняет (`keep_layers`).
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from connectors.ostrovok_site import ONSITE, amenity_facts
from core.packs import PackSource, add_layer, iter_jsonl, sha256_file
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/hotel_amenities.jsonl.gz"
CITIES = ("moscow", "sochi")


def build(snapshot: Path, city: str, packs: Path = PACKS) -> list[dict]:
    pack = packs / city
    hotel_ids = {h["id"] for h in iter_jsonl(pack / "data/hotels.jsonl.gz")}
    rows = sorted(
        (fact_row(f) for f in amenity_facts(snapshot, city, hotel_ids)),
        key=lambda r: (r["hotel_id"], r["feature_id"], str(r["params"]), r["fact_id"]),
    )
    write_jsonl_gz(pack / LAYER, rows)
    source = PackSource(
        id="hotel_amenities:ostrovok_site",
        provider="ostrovok.ru",
        release=snapshot.name,
        sha256=sha256_file(snapshot / "manifests/pages.jsonl"),
        license="Страницы Островка; сбор разрешён организаторами (D24)",
    )
    add_layer(pack, LAYER, "facts", len(rows), [source])
    stats = Counter(
        (r["feature_id"], (r["params"] or {}).get("category") if r["feature_id"] == ONSITE
         else next(iter(r["value"].values())))
        for r in rows
    )  # fmt: skip
    hotels = len({r["hotel_id"] for r in rows})
    print(f"{city}: {len(rows)} фактов у {hotels} из {len(hotel_ids)} отелей")
    for key, n in sorted(stats.items(), key=str):
        print(f"  {key}: {n}")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True, type=Path, help="снимок Островка 24.10")
    ap.add_argument("--city", choices=CITIES, nargs="*", default=list(CITIES))
    args = ap.parse_args()
    for city in args.city:
        build(args.snapshot.resolve(), city)


if __name__ == "__main__":
    main()
