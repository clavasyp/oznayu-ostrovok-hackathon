"""Места и станции пакета заново из среза OSM — без полной пересборки (`finalize_packs`).

Вход — поставляемый пакет города и срез `connectors.osm_pbf` (полный или `--places-only`) того же
PBF. Таблицы `places`, `transit_stations`, `hotel_stations` и `transit_times` пересчитывает
`pipeline.nearby_tables.build` по отелям пакета; остальные файлы, слои калькуляторов и их
источники не меняются. В манифесте — строки, байты и sha256 файлов, источник `d34_places_osm`
и версия пакета `<code_version>-<хеш файлов>[+<хеш слоёв>]`. Пакет меняется на месте — запускать
на рабочем дереве git или копии. Повтор на тех же входах даёт те же байты.

    python -m pipeline.refresh_places --pack packs/sochi --osm-dir /tmp/osm/sochi \\
        --built-at 2026-10-28T03:00:00Z --report pipeline/reports/places_refresh.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from pipeline.nearby_tables import VERSION as NEARBY_VERSION
from pipeline.nearby_tables import build

VERSION = "places-refresh.1"
KINDS = ("places", "transit_stations", "hotel_stations", "transit_times")
SORT = {"hotel_stations": ("hotel_id", "station_id")}  # как у `build_packs.write_pack`
NOTE = (
    " Места и станции — places-refresh.1: парки с точками края через 150 м, подъёмники и "
    "аттракционы; места и станции за границей РФ не берутся."
)


def _key(kind: str, row: dict) -> tuple[str, ...]:
    return tuple(str(row.get(k, "")) for k in SORT.get(kind, ("id",)))


def _diff(kind: str, old: list[dict], new: list[dict]) -> dict:
    before = {_key(kind, r): r for r in old}
    after = {_key(kind, r): r for r in new}
    same = sum(1 for k, r in after.items() if before.get(k) == r)
    changed = [k for k, r in after.items() if k in before and before[k] != r]

    def values(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "source"}

    return {
        "before": len(old),
        "after": len(new),
        "unchanged": same,
        "changed": len(changed),
        "changed_besides_source": sum(values(before[k]) != values(after[k]) for k in changed),
        "removed": len(before.keys() - after.keys()),
        "added": len(after.keys() - before.keys()),
    }


def refresh(pack: Path, osm_dir: Path, *, built_at: datetime) -> dict:
    if built_at.utcoffset() is None:
        raise ValueError("Build timestamp requires timezone")
    manifest = verify(pack)
    city = manifest.city_id
    osm = json.loads((osm_dir / "manifest.json").read_text("utf-8"))
    if osm["city_id"] != city:
        raise PackError(f"срез OSM {osm['city_id']} не для пакета {city}")
    hotels = [h for f in manifest.files if f.kind == "hotels" for h in iter_jsonl(pack / f.path)]
    parts: dict[str, list[dict]] = {"hotels": hotels}
    nearby = build(parts, osm_dir, city)
    report = {"city": city, "pack_version_before": manifest.pack_version, "nearby": nearby}
    files = list(manifest.files)
    for kind in KINDS:
        # 2ГИС (D37) — свои файлы `_2gis`: их этот шаг не трогает.
        entries = [f for f in files if f.kind == kind and not f.layer and "_2gis" not in f.path]
        if len(entries) != 1:
            raise PackError(f"{city}: ожидался один файл {kind}, найдено {len(entries)}")
        entry = entries[0]
        rows = sorted(parts[kind], key=lambda r: _key(kind, r))
        if len(rows) > 250000:
            raise PackError(f"{city}: {kind} больше 250 000 строк — нужна разбивка write_pack")
        report[kind] = _diff(kind, list(iter_jsonl(pack / entry.path)), rows)
        path = pack / entry.path
        write_jsonl_gz(path, rows)
        files[files.index(entry)] = entry.model_copy(
            update={"rows": len(rows), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    sources = [s for s in manifest.sources if s.id != "d34_places_osm"]
    sources.append(
        PackSource(
            id="d34_places_osm",
            provider="OpenStreetMap/Geofabrik",
            release=osm["source_date"],
            sha256=osm["source_sha256"],
            license="ODbL-1.0",
            commercial_ok=True,
            extract=osm["algorithm_version"],
            method=f"{NEARBY_VERSION}; {VERSION}",
            region_relations=osm.get("region_relation_ids") or None,
        )
    )
    identity = (manifest.code_version or "") + "".join(f.sha256 for f in files if not f.layer)
    base = f"{manifest.code_version}-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
    notes = manifest.notes or ""
    manifest = manifest.model_copy(
        update={
            "files": files,
            "sources": sources,
            "built_at": built_at,
            "pack_version": base,
            "notes": notes if NOTE.strip() in notes else notes + NOTE,
        }
    )
    manifest = manifest.model_copy(update={"pack_version": _layers_version(manifest)})
    write_manifest(pack, manifest)
    verify(pack)
    listed = {f.path for f in manifest.files} | {"manifest.json"}
    on_disk = {str(p.relative_to(pack)) for p in pack.rglob("*") if p.is_file()}
    if on_disk != listed:
        raise PackError(f"{city}: файлы пакета и манифеста расходятся: {on_disk ^ listed}")
    report["pack_version"] = manifest.pack_version
    report["place_categories"] = dict(
        sorted(Counter(c for p in parts["places"] for c in p["categories"]).items())
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pack", type=Path, required=True, action="append")
    p.add_argument("--osm-dir", type=Path, required=True, action="append")
    p.add_argument("--built-at", type=datetime.fromisoformat, default=None)
    p.add_argument("--report", type=Path)
    a = p.parse_args()
    if len(a.pack) != len(a.osm_dir):
        p.error("--pack и --osm-dir парами")
    built_at = a.built_at or datetime.now(UTC)
    reports = [
        refresh(pack, osm, built_at=built_at) for pack, osm in zip(a.pack, a.osm_dir, strict=True)
    ]
    text = json.dumps(reports, ensure_ascii=False, indent=2) + "\n"
    if a.report:
        a.report.write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
