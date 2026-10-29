"""Высоты зданий 3D-GloBFP для сцены вида и солнца: контуры вокруг корпусов отелей → JSONL.

    python -m pipeline.extract_globfp --zip <тайл 1>.zip --zip <тайл 2>.zip --pack packs/moscow \\
        --out <globfp_moscow.jsonl>

Тайлы 3D-GloBFP v2 — zip с shapefile (figshare, CC BY 4.0; `view_sun.URLS["globfp"]`). Формат
строки — как у файла Сочи (`globfp_sochi.jsonl`): `geometry_wkb` (hex), `attrs` (поля shapefile,
высота — `Height`), `source_file`. Берём контуры в радиусе `BUILDINGS_M` от корпусов отелей пакета
— там же, где сцена (`pipeline.build_scene_osm`). Рядом — `<out>.meta.json`: тайлы, их md5 и
sha256, число контуров. Чтение shapefile — пакет `pyshp` (только для этой сборки, в образе его нет).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from calculators.view_sun import BUILDINGS_M
from core.packs import sha256_file
from pipeline.build_scene_osm import _envelopes


def extract(zips: list[Path], pack: Path, out: Path, radius_m: float = BUILDINGS_M) -> dict:
    import shapefile  # pyshp
    from shapely.geometry import box, shape

    tree, bounds, _ = _envelopes(pack, radius_m)
    frame = box(*bounds)
    counts: dict[str, int] = {}
    tmp = out.with_suffix(".part")
    with tmp.open("w", encoding="utf-8") as f:
        for z in zips:
            n = 0
            with tempfile.TemporaryDirectory() as folder, zipfile.ZipFile(z) as archive:
                shp = next(name for name in archive.namelist() if name.endswith(".shp"))
                archive.extractall(folder)
                reader = shapefile.Reader(str(Path(folder) / shp))
                if not frame.intersects(box(*reader.bbox)):
                    counts[z.name] = 0
                    continue
                for sr in reader.iterShapeRecords(bbox=list(bounds)):
                    g = shape(sr.shape.__geo_interface__)
                    if g.is_empty or not len(tree.query(g, predicate="intersects")):
                        continue
                    row = {"geometry_wkb": g.wkb_hex, "attrs": sr.record.as_dict(),
                           "source_file": z.name}  # fmt: skip
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    n += 1
            counts[z.name] = n
    tmp.replace(out)
    meta = {
        "tiles": [
            {
                "name": z.name,
                "md5": hashlib.md5(z.read_bytes()).hexdigest(),
                "sha256": sha256_file(z),
                "footprints": counts[z.name],
            }
            for z in zips
        ],  # fmt: skip
        "release": "3D-GloBFP v2 (2025-05-22)",
        "radius_m": radius_m,
        "footprints": sum(counts.values()),
        "sha256": sha256_file(out),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    out.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", type=Path, action="append", required=True)
    ap.add_argument("--pack", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    print(json.dumps(extract(args.zip, args.pack, args.out), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
