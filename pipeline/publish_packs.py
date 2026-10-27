"""Публикация проверенных фокусных пакетов в packs/ с сохранением прежних файлов вне git."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from core.packs import keep_layers, refresh_coverage, verify, write_manifest
from pipeline.build_v9_packs import PACKS, ROOT


def publish(city_dirs: list[Path], backup: Path) -> dict:
    backup = backup.resolve()
    if backup.is_relative_to(ROOT) or ROOT.is_relative_to(backup):
        raise ValueError("Backup must be outside the repository")
    if backup.exists():
        raise ValueError("Use a new backup directory")
    sources = [(p.resolve(), verify(p)) for p in city_dirs]
    ids = [m.city_id for _, m in sources]
    if len(set(ids)) != len(ids) or any(city not in {"moscow", "sochi"} for city in ids):
        raise ValueError("Only distinct focus city packs can be published")
    backup.mkdir(parents=True)
    if backup.stat().st_dev != PACKS.stat().st_dev:
        raise ValueError("Atomic directory replacement requires backup on the same filesystem")
    result = {}
    for source, manifest in sources:
        target = PACKS / manifest.city_id
        verify(target)
        # Слои калькуляторов (вид и солнце, `add_layer`) — отдельная сборка: из прежнего пакета.
        built = {entry.path for entry in manifest.files}
        manifest = keep_layers(target, manifest)
        stage = PACKS / (".seva-stage-" + uuid4().hex)
        stage.mkdir()
        for entry in manifest.files:
            path = stage / entry.path
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2((source if entry.path in built else target) / entry.path, path)
        if any(entry.path not in built for entry in manifest.files):
            manifest = refresh_coverage(stage, manifest)  # покрытие — вместе со слоями
        write_manifest(stage, manifest)
        verify(stage)
        old = backup / manifest.city_id
        os.replace(target, old)
        try:
            os.replace(stage, target)
        except BaseException:
            os.replace(old, target)
            raise
        result[manifest.city_id] = {
            "pack_version": manifest.pack_version,
            "bytes": sum(f.bytes for f in manifest.files),
        }
    (backup / "published.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city-dir", action="append", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.city_dir, args.backup), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
