"""Слой «покрытие пляжа» (B01) для отелей Сочи — по ближайшему пляжу из расчёта W01.

    python -m pipeline.build_beach_surface --pbf <срез Geofabrik ЮФО>

Пляж — тот, до которого считано «пешком до пляжа» (W01, `marine_classification.beach_id`):
ближайший морской пляж OSM. Покрытие — его тег `surface` из того же среза Geofabrik, что у
пакета: галька, гравий, валуны — `pebble`, песок — `sand`, скала — `rock`. Нет тега или покрытие
не из словаря (бетонные плиты, настил) — факта нет: «неизвестно», а не догадка (D5). Это оценка
(D18), как и сам W01: рядом может быть другой пляж с другим покрытием, а тег — правка карты, а не
осмотр. «Обязательно песчаный пляж» оценка не закрывает, но поднимает в выдаче подходящие отели.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from core.facts import Fact, make_fact_id
from core.packs import PackSource, add_layer, iter_jsonl, read_manifest, sha256_file
from pipeline.build_v9_packs import PACKS, write_jsonl_gz
from pipeline.build_view_sun import fact_row

LAYER = "data/beach_surface.jsonl.gz"
VERSION = "beach-surface-osm.1"
SURFACE = {
    "sand": "sand",
    "pebblestone": "pebble",
    "pebbles": "pebble",
    "shingle": "pebble",
    "gravel": "pebble",
    "fine_gravel": "pebble",
    "stone": "pebble",
    "rock": "rock",
}
WORDS = {"sand": "песок", "pebble": "галька", "rock": "скалы"}


def nearest_beaches(pack: Path) -> dict[str, dict]:
    """Отель → ближайший пляж из фактов W01 (id, название, расстояние)."""
    out: dict[str, dict] = {}
    for f in read_manifest(pack).files:
        if f.kind != "facts":
            continue
        for r in iter_jsonl(pack / f.path):
            if r["feature_id"] != "W01" or not r.get("value"):
                continue
            beach = (r["value"].get("marine_classification") or {}).get("beach_id")
            if beach and r["hotel_id"] not in out:
                out[r["hotel_id"]] = {
                    "beach_id": beach,
                    "name": r["value"]["marine_classification"].get("beach_name"),
                    "distance_m": r["value"].get("distance_m"),
                }
    return out


def beach_tags(pbf: Path, ids: set[str]) -> tuple[dict[str, dict], str | None]:
    """Теги пляжей `natural=beach` из среза PBF: только нужные id, без координат."""
    import osmium

    processor = osmium.FileProcessor(pbf, osmium.osm.WAY | osmium.osm.RELATION).with_filter(
        osmium.filter.TagFilter(("natural", "beach"))
    )
    date = processor.header.get("osmosis_replication_timestamp")
    tags = {}
    for obj in processor:
        key = ("way/" if obj.is_way() else "relation/") + str(obj.id)
        if key in ids:
            tags[key] = dict(obj.tags)
    return tags, date


def surface_facts(
    city: str, beaches: dict[str, dict], tags: dict[str, dict], meta: dict, built_at: datetime
) -> list[dict]:
    rows = []
    for hid, beach in sorted(beaches.items()):
        raw = (tags.get(beach["beach_id"]) or {}).get("surface")
        surface = SURFACE.get(raw or "")
        if surface is None:
            continue
        name = beach["name"] or "без названия"
        distance = f", {round(beach['distance_m'])} м пешком" if beach.get("distance_m") else ""
        fact = Fact(
            fact_id=make_fact_id(hid, f"hotel:{hid}", "B01", {}, VERSION),
            hotel_id=hid,
            city_id=city,
            subject_level="hotel",
            subject_id=f"hotel:{hid}",
            feature_id="B01",
            value={"surface": surface, "beach_id": beach["beach_id"], "beach_name": beach["name"]},
            status="estimate",
            sources=[
                {
                    "provider": "OpenStreetMap",
                    "url": f"https://www.openstreetmap.org/{beach['beach_id']}",
                    "license": "ODbL-1.0",
                    "source_value": raw,
                    "dataset_sha256": meta["pbf_sha256"],
                    "dataset_version": meta["source_date"],
                }
            ],  # fmt: skip
            recipe_id="B01-OSM-nearest-beach-surface",
            algorithm_version=VERSION,
            confidence={
                "level": "low",
                "reason": "Тег surface ближайшего пляжа в OSM — правка карты, а не осмотр; рядом "
                "может быть другой пляж с другим покрытием.",
            },
            calculated_at=built_at,
            scope_note=f"Ближайший пляж «{name}»{distance}: {WORDS[surface]} по OSM.",
            hard_condition_eligible=False,
        )
        rows.append(fact_row(fact))
    return rows


def build(pbf: Path, city: str = "sochi", packs: Path = PACKS) -> list[dict]:
    pack = packs / city
    manifest = read_manifest(pack)
    beaches = nearest_beaches(pack)
    tags, date = beach_tags(pbf, {b["beach_id"] for b in beaches.values()})
    meta = {"pbf_sha256": sha256_file(pbf), "source_date": date}
    rows = surface_facts(city, beaches, tags, meta, manifest.built_at or datetime.now(UTC))
    write_jsonl_gz(pack / LAYER, rows)
    source = PackSource(
        id="beach_surface:osm",
        provider="OpenStreetMap/Geofabrik (теги пляжей)",
        release=date,
        sha256=meta["pbf_sha256"],
        license="ODbL-1.0",
        commercial_ok=True,
    )
    add_layer(pack, LAYER, "facts", len(rows), [source])
    found = Counter(r["value"]["surface"] for r in rows)
    print(f"{city}: пляжей у W01 {len({b['beach_id'] for b in beaches.values()})}, с тегом "
          f"surface {len(tags and {k for k, t in tags.items() if t.get('surface')})}; "
          f"фактов B01 {len(rows)} из {len(beaches)} отелей: {dict(found)}")  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--pbf", type=Path, required=True, help="срез Geofabrik ЮФО (тот же, что у пакета)"
    )
    ap.add_argument("--city", default="sochi")
    args = ap.parse_args()
    build(args.pbf, args.city)


if __name__ == "__main__":
    main()
