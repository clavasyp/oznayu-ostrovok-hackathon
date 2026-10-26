"""Контракт 5: пакет города (core.md §9, D6). Формат описан в packs/FORMAT.md.

Пакет собирается заранее (Сева, профиль `full`) и лежит в репозитории. Сервис `init` у жюри
только проверяет хеши и грузит его в базу — без сети, графов и растров.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_VERSION = "1"
FileKind = Literal[
    "hotels",
    "room_types",
    "placement_sets",
    "placements",
    "placement_windows",
    "buildings",
    "hotel_buildings",
    "facts",
    "questions",
    "coverage",
    "ota_comparisons",
    "other",
]


class PackSource(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    provider: str
    url: str | None = None
    release: str | None = None
    sha256: str | None = None
    license: str | None = None
    commercial_ok: bool | None = None
    fetched_at: datetime | None = None


class PackFile(BaseModel):
    path: str  # относительно папки пакета, например "data/facts.jsonl.gz"
    kind: FileKind
    rows: int
    bytes: int
    sha256: str


class Manifest(BaseModel):
    format_version: Literal["1"] = FORMAT_VERSION
    city_id: str
    city_name: str
    timezone: str
    depth: Literal["full", "automatic"]  # D36: Москва и Сочи — full, остальные — automatic
    pack_version: str
    built_at: datetime
    code_version: str | None = None  # git sha сборки
    catalog_version: str | None = None
    sources: list[PackSource] = Field(default_factory=list)
    files: list[PackFile]
    notes: str | None = None


class PackError(Exception):
    pass


def stable_id(*parts: str) -> int:
    """Стабильный BIGINT из текста: повторная загрузка того же пакета обновляет, а не дублирует.

    60 бит — больше 2^53, поэтому в JSON для браузера такие id отдаются строкой (API)."""
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:15], 16)


# Служебные пути v9 в `params`: не смысл условия, а файл, из которого взят расчёт.
PROVENANCE_PARAMS = ("fan_file", "target_source_path", "quality_exclusion_evidence")


def repo_relative(path: str) -> str:
    """Путь с машины разработчика → путь в материалах расчётов («research_notes/…» от папки
    проекта). Не все такие файлы скопированы в репозиторий: например, `results/` v9 — нет."""
    i = path.find("research_notes/")
    return path[i:] if i >= 0 else Path(path).name


_LOCAL_ROOTS = ("/Users/", "/home/", "/private/", "/tmp/")  # машина разработчика, не данные


def _relative_paths(x: Any) -> Any:
    """Все строки-пути с машины разработчика → пути в материалах расчётов (паспорта v9:
    `sources[].snapshot_file`, `depends_on[].file` и т. п.)."""
    if isinstance(x, str):
        return repo_relative(x) if x.startswith(_LOCAL_ROOTS) else x
    if isinstance(x, dict):
        return {k: _relative_paths(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_relative_paths(v) for v in x]
    return x


def move_provenance_params(fact: dict[str, Any]) -> dict[str, Any]:
    """Пути к файлам расчёта — из `params` в поле факта `evidence_files`, остальные пути
    с машины разработчика в паспорте — в виде «research_notes/…».

    В `params` пути показывались на экранах («Параметры») и разносили по пакетам путь и имя
    пользователя разработчика. Ключ из `PROVENANCE_PARAMS` уходит из `params` при любом
    значении (и `null`). Прочие служебные поля в `params` (у B02 — координаты API, хеши) —
    отдельная задача. Любой другой абсолютный путь в `params` и оставшийся локальный путь —
    ошибка сборки."""
    params = dict(fact.get("params") or {})
    found = {k: params.pop(k) for k in PROVENANCE_PARAMS if k in params}
    moved = {k: repo_relative(v) for k, v in found.items() if isinstance(v, str)}
    leaked = [
        k for k, v in params.items() if isinstance(v, str) and (v.startswith("/") or "/Users/" in v)
    ]
    if leaked:
        raise PackError(f"факт {fact.get('fact_id')}: абсолютный путь в params: {leaked}")
    out = _relative_paths(fact | {"params": params})
    if moved:
        out["evidence_files"] = moved
    if any(root in json.dumps(out, ensure_ascii=False) for root in _LOCAL_ROOTS):
        raise PackError(f"факт {fact.get('fact_id')}: в паспорте остался локальный путь")
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(pack_dir: Path) -> Manifest:
    return Manifest.model_validate_json((pack_dir / "manifest.json").read_text(encoding="utf-8"))


def verify(pack_dir: Path) -> Manifest:
    """Проверка целостности: каждый файл на месте, размер и sha256 совпадают."""
    manifest = read_manifest(pack_dir)
    for f in manifest.files:
        path = pack_dir / f.path
        if not path.is_file():
            raise PackError(f"{manifest.city_id}: нет файла {f.path}")
        if path.stat().st_size != f.bytes:
            raise PackError(f"{manifest.city_id}: размер {f.path} не совпадает с манифестом")
        if sha256_file(path) != f.sha256:
            raise PackError(f"{manifest.city_id}: sha256 {f.path} не совпадает с манифестом")
    return manifest


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def discover(packs_root: Path) -> list[Path]:
    return sorted(p.parent for p in packs_root.glob("*/manifest.json"))
