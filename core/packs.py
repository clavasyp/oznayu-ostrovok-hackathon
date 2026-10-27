"""Контракт 5: пакет города (core.md §9, D6). Формат описан в packs/FORMAT.md.

Пакет собирается заранее (Сева, профиль `full`) и лежит в репозитории. Сервис `init` у жюри
только проверяет хеши и грузит его в базу — без сети, графов и растров.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
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
    "facades",
    "places",
    "transit_stations",
    "hotel_stations",
    "transit_times",
    "curation_items",
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
    layer: bool | None = None  # файл-слой отдельного калькулятора (`add_layer`), не сборки v9


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


def _layers_version(manifest: Manifest) -> str:
    """Версия пакета: версия сборки v9 плюс хеш файлов-слоёв (`v9-…+xxxxxxxx`)."""
    base = manifest.pack_version.split("+")[0]
    layers = sorted(f.sha256 for f in manifest.files if f.layer)
    if not layers:
        return base
    return f"{base}+{hashlib.sha256(''.join(layers).encode()).hexdigest()[:8]}"


def add_layer(
    pack_dir: Path, path: str, kind: FileKind, rows: int, sources: list[PackSource]
) -> Manifest:
    """Добавить или заменить в манифесте файл-слой: его собирает отдельный калькулятор (вид и
    солнце), а не сборка v9. Файл помечен `layer`, его источники — путём файла в `layer`;
    id источников у каждого слоя свои (префикс слоя). Сборка v9 слои сохраняет (`keep_layers`).
    К версии пакета добавляется хеш слоёв: пакет с другим слоем — другая версия. Покрытие
    (`coverage`) пересчитывается по всем фактам вместе со слоем."""
    manifest = read_manifest(pack_dir)
    file = pack_dir / path
    entry = PackFile(
        path=path,
        kind=kind,
        rows=rows,
        bytes=file.stat().st_size,
        sha256=sha256_file(file),
        layer=True,
    )
    files = [f for f in manifest.files if f.path != path] + [entry]
    own = {s.id for s in sources}
    clash = [s.id for s in manifest.sources if s.id in own and getattr(s, "layer", None) != path]
    if clash:
        raise PackError(f"{manifest.city_id}: id источников уже заняты другим слоем: {clash}")
    kept = [s for s in manifest.sources if getattr(s, "layer", None) != path]
    marked = [PackSource.model_validate(s.model_dump() | {"layer": path}) for s in sources]
    manifest = manifest.model_copy(update={"files": files, "sources": kept + marked})
    if kind == "facts":  # покрытие — по всем фактам, со слоем
        manifest = refresh_coverage(pack_dir, manifest)
    manifest = manifest.model_copy(update={"pack_version": _layers_version(manifest)})
    write_manifest(pack_dir, manifest)
    return manifest


def write_jsonl_gz(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """gzip без времени в заголовке — иначе sha256 менялся бы при каждой сборке."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        for row in rows:
            gz.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode())
    path.write_bytes(buf.getvalue())


COVERAGE_KEYS = ("computed", "estimate", "unknown", "not_applicable")


def coverage_rows(
    hotel_ids: Iterable[str], facts: Iterable[dict[str, Any]], templates: Iterable[dict]
) -> list[dict[str, Any]]:
    """Покрытие характеристик по фактам: computed, observed или curated → computed; иначе
    estimate → estimate; только not_applicable → not_applicable; прочее → unknown.
    Знаменатель — все отели пакета; поля строки-шаблона (название и т. п.) сохраняются."""
    ids = set(hotel_ids)
    by_feature: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for fact in facts:
        if fact["hotel_id"] in ids:
            by_feature[fact["feature_id"]][fact["hotel_id"]].add(fact["status"])
    result = []
    for template in templates:
        counts: Counter[str] = Counter()
        for hid in ids:
            statuses = by_feature[template["feature_id"]].get(hid, set())
            if statuses & {"computed", "observed", "curated"}:
                counts["computed"] += 1
            elif "estimate" in statuses:
                counts["estimate"] += 1
            elif "not_applicable" in statuses and not statuses - {"not_applicable"}:
                counts["not_applicable"] += 1
            else:
                counts["unknown"] += 1
        result.append(template | {"n_hotels": len(ids), **{k: counts[k] for k in COVERAGE_KEYS}})
    return sorted(result, key=lambda r: r["feature_id"])


def refresh_coverage(pack_dir: Path, manifest: Manifest) -> Manifest:
    """Пересчитать `coverage` пакета по всем файлам фактов, включая слои: иначе после
    `add_layer` / `keep_layers` покрытие V01 / V05 расходится с фактами (аудит пакетов).

    Пакет без файла покрытия или без отелей не меняется."""
    entries = [f for f in manifest.files if f.kind == "coverage"]
    hotel_files = [f for f in manifest.files if f.kind == "hotels"]
    if len(entries) != 1 or not hotel_files:
        return manifest
    entry = entries[0]
    path = pack_dir / entry.path
    templates = list(iter_jsonl(path))
    ids = [h["id"] for f in hotel_files for h in iter_jsonl(pack_dir / f.path)]
    facts = (
        row for f in manifest.files if f.kind == "facts" for row in iter_jsonl(pack_dir / f.path)
    )
    rows = coverage_rows(ids, facts, templates)
    if rows == sorted(templates, key=lambda r: r["feature_id"]):
        return manifest
    write_jsonl_gz(path, rows)
    updated = entry.model_copy(
        update={"rows": len(rows), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
    )
    files = [updated if f.path == entry.path else f for f in manifest.files]
    return manifest.model_copy(update={"files": files})


def keep_layers(pack_dir: Path, manifest: Manifest) -> Manifest:
    """Пересборка пакета: файлы-слои и их источники из прежнего манифеста — в новый.

    Слой, чей файл исчез, выпадает вместе с источниками."""
    path = pack_dir / "manifest.json"
    if not path.is_file():
        return manifest
    old = read_manifest(pack_dir)
    paths = {f.path for f in manifest.files}
    files = [
        f for f in old.files if f.layer and f.path not in paths and (pack_dir / f.path).is_file()
    ]
    layers = {f.path for f in files}
    sources = [s for s in old.sources if getattr(s, "layer", None) in layers]
    manifest = manifest.model_copy(
        update={"files": manifest.files + files, "sources": manifest.sources + sources}
    )
    return manifest.model_copy(update={"pack_version": _layers_version(manifest)})


def write_manifest(pack_dir: Path, manifest: Manifest) -> None:
    (pack_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2, exclude_none=True) + "\n", "utf-8"
    )


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
