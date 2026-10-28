"""Районы Москвы у отелей: `subzone` — район для гостя («Хамовники»), `subzone_id` — код.

    python -m pipeline.build_districts extract --pbf <срез Geofabrik ЦФО> --raw <новая папка>
    python -m pipeline.build_districts apply --raw <папка> [--pack packs/moscow]

Границы — отношения OSM `boundary=administrative` из того же среза Geofabrik, что у слоёв
зданий: административные округа (admin_level 5) и районы и поселения (admin_level 8) внутри них.
Отель получает район, в полигоне которого лежит его точка; вне Москвы (у аэропортов в области)
района нет — фильтр по району такой отель не возьмёт. Список районов с округами — в
`agents/search/moscow_areas.json`: по нему разбор понимает «в Хамовниках», «в ЦАО», «в Новой
Москве», как районы Сочи. Пакет меняется на месте (файл отелей, источник, версия), как у
`refresh_places`; повтор на тех же входах даёт те же байты.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from core.packs import (
    PackError,
    PackSource,
    _layers_version,
    iter_jsonl,
    sha256_file,
    verify,
    write_jsonl_gz,
    write_manifest,
)
from pipeline.build_v9_packs import PACKS

VERSION = "districts-osm.1"
AREAS_FILE = Path(__file__).resolve().parents[1] / "agents/search/moscow_areas.json"
MOSCOW_BOX = (36.8, 55.1, 37.97, 56.03)  # старая и новая Москва с Зеленоградом
SIMPLIFY_DEG = 0.00005  # ≈ 5 м: граница района проходит по улице, точность отеля хуже
OKRUG_SHORT = {
    "Центральный": "ЦАО", "Северный": "САО", "Северо-Восточный": "СВАО", "Восточный": "ВАО",
    "Юго-Восточный": "ЮВАО", "Южный": "ЮАО", "Юго-Западный": "ЮЗАО", "Западный": "ЗАО",
    "Северо-Западный": "СЗАО", "Зеленоградский": "ЗелАО", "Новомосковский": "НАО",
    "Троицкий": "ТАО",
}  # fmt: skip
NOTE = " Районы Москвы — districts-osm.1: границы районов и поселений OSM (admin_level 8)."
_LAT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya".split(),
    strict=True,
)) | {"ъ": "", "ь": ""}  # fmt: skip


def slug(name: str) -> str:
    """«Хорошёво-Мнёвники» → `khoroshevo_mnevniki`: код района латиницей, стабильный."""
    out = "".join(_LAT.get(c, c) for c in name.lower()).replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", re.sub(r"_+", "_", out)).strip("_")


def short_name(name: str) -> str:
    """Название для гостя без служебных слов: «район Хамовники» → «Хамовники», «Басманный
    район» → «Басманный», «поселение Сосенское» → «Сосенское»."""
    bare = re.sub(r"\b(?:район|поселение|муниципальный округ)\b", "", name)
    return re.sub(r"\s+", " ", bare).strip()


def extract(pbf: Path, raw: Path) -> dict:
    """Границы округов и районов Москвы из среза PBF → `raw/districts.geojson` и `meta.json`.
    Координаты узлов — в файловом индексе, как у `build_building_links.extract_pbf`."""
    import osmium
    from shapely import from_wkb, make_valid
    from shapely.geometry import box, mapping

    if (raw / "meta.json").exists():
        raise PackError(f"в {raw} уже есть выгрузка — для нового среза нужна новая папка")
    raw.mkdir(parents=True, exist_ok=True)
    frame = box(*MOSCOW_BOX)
    factory = osmium.geom.WKBFactory()
    index = raw / "nodes.idx"
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations(f"sparse_file_array,{index}")
        .with_areas(osmium.filter.TagFilter(("boundary", "administrative")))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA))
    )
    source_date = processor.header.get("osmosis_replication_timestamp")
    okrugs, districts = [], []
    try:
        for obj in processor:
            tags = dict(obj.tags)
            level = tags.get("admin_level")
            if obj.from_way() or level not in ("5", "8"):
                continue
            try:
                geom = from_wkb(factory.create_multipolygon(obj))
            except RuntimeError:
                continue  # контур не замкнут в срезе
            geom = make_valid(geom) if not geom.is_valid else geom
            if not geom.representative_point().within(frame):
                continue
            row = {"osm_relation": obj.orig_id(), "name": tags.get("name", ""),
                   "admin_level": level, "geometry": geom}  # fmt: skip
            if level == "5" and "административный округ" in row["name"]:
                okrugs.append(row)
            elif level == "8":
                districts.append(row)
    finally:
        index.unlink(missing_ok=True)
    features = []
    for d in districts:
        point = d["geometry"].representative_point()
        okrug = next((o for o in okrugs if o["geometry"].covers(point)), None)
        if okrug is None:
            continue  # поселение области в рамке — не Москва
        features.append({
            "type": "Feature",
            "geometry": mapping(d["geometry"].simplify(SIMPLIFY_DEG)),
            "properties": {"osm_relation": d["osm_relation"], "name": d["name"],
                           "okrug": okrug["name"], "okrug_relation": okrug["osm_relation"]},
        })  # fmt: skip
    features.sort(key=lambda f: f["properties"]["osm_relation"])
    path = raw / "districts.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                               ensure_ascii=False) + "\n", "utf-8")  # fmt: skip
    meta = {
        "source": "https://download.geofabrik.de/russia/central-fed-district-latest.osm.pbf",
        "pbf_sha256": sha256_file(pbf),
        "source_date": source_date,
        "districts_sha256": sha256_file(path),
        "okrugs": len(okrugs),
        "districts": len(features),
        "extracted_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (raw / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return meta


def okrug_short(okrug: str) -> str:
    return OKRUG_SHORT.get(okrug.split(" административный")[0], okrug)


def areas_list(features: list[dict]) -> list[dict]:
    """Районы для разбора: код, название, округ (сокращение), отношение OSM."""
    rows = [
        {"id": slug(short_name(f["properties"]["name"])),
         "name": short_name(f["properties"]["name"]),
         "okrug": okrug_short(f["properties"]["okrug"]),
         "osm_relation": f["properties"]["osm_relation"]}
        for f in features
    ]  # fmt: skip
    ids = Counter(r["id"] for r in rows)
    if dup := [i for i, n in ids.items() if n > 1]:
        raise PackError(f"одинаковые коды районов: {dup}")
    return sorted(rows, key=lambda r: (r["okrug"], r["name"]))


def apply(raw: Path, pack: Path = PACKS / "moscow", areas_file: Path = AREAS_FILE) -> dict:
    """Район каждому отелю пакета Москвы; файл отелей, источник и версия пакета — на месте."""
    from shapely import STRtree
    from shapely.geometry import Point, shape

    meta = json.loads((raw / "meta.json").read_text("utf-8"))
    path = raw / "districts.geojson"
    if sha256_file(path) != meta["districts_sha256"]:
        raise PackError("districts.geojson не совпадает с meta.json")
    features = json.loads(path.read_text("utf-8"))["features"]
    areas = areas_list(features)
    by_relation = {a["osm_relation"]: a for a in areas}
    shapes = [shape(f["geometry"]) for f in features]
    relations = [f["properties"]["osm_relation"] for f in features]
    tree = STRtree(shapes)
    manifest = verify(pack)
    entries = [f for f in manifest.files if f.kind == "hotels" and not f.layer]
    if len(entries) != 1:
        raise PackError("в пакете должен быть один файл отелей")
    entry = entries[0]
    hotels = list(iter_jsonl(pack / entry.path))
    counts: Counter = Counter()
    for h in hotels:
        point = Point(float(h["longitude"]), float(h["latitude"]))
        hits = [relations[i] for i in tree.query(point, predicate="intersects")]
        source = dict(h.get("source") or {})
        source.pop("district", None)
        if len(hits) == 1:
            area = by_relation[hits[0]]
            h["subzone"], h["subzone_id"] = area["name"], area["id"]
            source["district"] = {
                "osm_relation": hits[0],
                "okrug": area["okrug"],
                "method": "osm_admin_level_8_polygon",
                "version": VERSION,
            }
        else:  # вне Москвы или на общей границе — район не назначаем
            h["subzone"] = h["subzone_id"] = None
        h["source"] = source
        counts[h["subzone_id"] or "—"] += 1
    write_jsonl_gz(pack / entry.path, hotels)
    file = pack / entry.path
    files = [
        f.model_copy(update={"rows": len(hotels), "bytes": file.stat().st_size,
                             "sha256": sha256_file(file)}) if f.path == entry.path else f
        for f in manifest.files
    ]  # fmt: skip
    sources = [s for s in manifest.sources if s.id != "districts_osm"]
    sources.append(
        PackSource(
            id="districts_osm",
            provider="OpenStreetMap/Geofabrik (границы районов Москвы)",
            release=meta["source_date"],
            sha256=meta["pbf_sha256"],
            license="ODbL-1.0",
            commercial_ok=True,
            method=f"{VERSION}; districts.geojson sha256 {meta['districts_sha256'][:12]}",
        )
    )
    identity = (manifest.code_version or "") + "".join(f.sha256 for f in files if not f.layer)
    base = f"{manifest.code_version}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    notes = manifest.notes or ""
    manifest = manifest.model_copy(
        update={
            "files": files,
            "sources": sources,
            "pack_version": base,
            "notes": notes if NOTE.strip() in notes else notes + NOTE,
        }  # fmt: skip
    )
    manifest = manifest.model_copy(update={"pack_version": _layers_version(manifest)})
    write_manifest(pack, manifest)
    verify(pack)
    areas_file.write_text(json.dumps(areas, ensure_ascii=False, indent=1) + "\n", "utf-8")
    found = len(hotels) - counts["—"]
    print(f"moscow: районов {len(areas)}; отелей с районом {found} из {len(hotels)}; "
          f"больше всего: {counts.most_common(4)}")  # fmt: skip
    return {"pack_version": manifest.pack_version, "hotels": len(hotels), "with_district": found}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("--pbf", type=Path, required=True)
    ex.add_argument("--raw", type=Path, required=True)
    ap_ = sub.add_parser("apply")
    ap_.add_argument("--raw", type=Path, required=True)
    ap_.add_argument("--pack", type=Path, default=PACKS / "moscow")
    args = ap.parse_args()
    if args.cmd == "extract":
        print(json.dumps(extract(args.pbf, args.raw), ensure_ascii=False, indent=2))
    else:
        apply(args.raw, args.pack)


if __name__ == "__main__":
    main()
