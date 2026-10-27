"""Офлайн-аудит готовых пакетов: хеши, строки, ссылки, D26/D33/D34/D39 и покрытие.

Не меняет пакеты и не подключается к БД/сети. Миллионы ключей хранятся во временной
SQLite, а примеры ошибок ограничены. --snapshot дополнительно проверяет первоисточник.
Если в пакете есть корпуса, ссылки на корпуса и фасады (размещения, окна, факты уровней
building / facade / facade_physical_floor) обязаны находиться в пакете. Исключение — файлы-слои
(`"layer": true` в манифесте): их субъекты пока могут быть в другой схеме id фасадов, это
предупреждение. Запуск по всем пакетам — `make audit`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from core.facts import Fact
from core.packs import read_manifest, sha256_file

VERSION = "pack-audit.3"
KEYS = {
    "hotels": ("id",),
    "room_types": ("id",),
    "buildings": ("id",),
    "hotel_buildings": ("hotel_id", "building_id"),
    "facades": ("building_id", "facade_id"),
    "places": ("id",),
    "transit_stations": ("id",),
    "hotel_stations": ("hotel_id", "station_id"),
    "transit_times": ("id",),
    "placement_sets": ("room_type_id",),
    "placements": ("id",),
    "placement_windows": ("placement_id", "facade_id"),
    "curation_items": ("id",),
    "facts": ("fact_id",),
    "questions": ("id",),
    "coverage": ("city_id", "feature_id"),
    "ota_comparisons": ("id",),
}
ORDER = (
    "hotels",
    "room_types",
    "buildings",
    "hotel_buildings",
    "facades",
    "places",
    "transit_stations",
    "hotel_stations",
    "transit_times",
    "placement_sets",
    "placements",
    "placement_windows",
    "curation_items",
    "facts",
    "questions",
    "coverage",
    "ota_comparisons",
)
REFERENCES = {
    "room_types": (("hotel_id", "hotels"),),
    "hotel_buildings": (("hotel_id", "hotels"), ("building_id", "buildings")),
    "facades": (("building_id", "buildings"),),
    "hotel_stations": (("hotel_id", "hotels"), ("station_id", "transit_stations")),
    "transit_times": (
        ("source_station_id", "transit_stations"),
        ("target_station_id", "transit_stations"),
    ),
    "placement_sets": (("room_type_id", "room_types"),),
    "placements": (("room_type_id", "placement_sets"),),
    "placement_windows": (("placement_id", "placements"),),
    "facts": (("hotel_id", "hotels"),),
    "questions": (("hotel_id", "hotels"),),
    "ota_comparisons": (("hotel_id", "hotels"),),
}
PARENTS = {parent for refs in REFERENCES.values() for _, parent in refs}
SIDES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
GEOMETRY_LEVELS = {"building", "facade", "facade_physical_floor"}
FLOOR = re.compile(r"floor-?[0-9]+")


def geometry_subject(level, subject_id):
    """`<building>` / `<building>:<facade>` / `<building>:<facade>:floorN` → (здание, фасад).

    В схеме v9 id фасада сам содержит двоеточия (`p0:r0:e0`), поэтому этаж отрезается справа,
    а здание — до первого двоеточия."""
    if level == "building":
        return subject_id, None
    if level == "facade_physical_floor":
        subject_id, floor = subject_id.rsplit(":", 1)
        if not FLOOR.fullmatch(floor):
            raise ValueError("floor suffix required")
    building, facade = subject_id.split(":", 1)
    return building, facade


def passports(row):
    """Паспорта строки: `source` у мест и связей, список `sources` у фактов."""
    found = [row["source"]] if isinstance(row.get("source"), dict) else []
    found += [s for s in row.get("sources") or [] if isinstance(s, dict)]
    return found


def is_twogis(row):
    return any(source.get("provider") == "2gis" for source in passports(row))


class Issues:
    def __init__(self, limit=50):
        self.errors = Counter()
        self.warnings = Counter()
        self.samples = []
        self.limit = limit

    def add(self, code, where, *, warning=False, field=None):
        (self.warnings if warning else self.errors)[code] += 1
        if len(self.samples) < self.limit:
            self.samples.append(
                {
                    "code": code,
                    "where": where,
                    "severity": "warning" if warning else "error",
                    "field": field,
                }
            )

    def dump(self):
        return {
            "errors": dict(self.errors),
            "warnings": dict(self.warnings),
            "samples": self.samples,
        }


def number(value, low=0, high=math.inf):
    try:
        return (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and low <= value <= high
        )
    except OverflowError:
        return False


def invalid_constant(value):
    raise ValueError("non-finite JSON constant")


def aware_date(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None or date.utcoffset() is None:
        raise ValueError("timezone required")
    return date


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("relative path required")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("path outside pack")
    return path


def records(path, relative, issues):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                where = f"{relative}:{line_number}"
                try:
                    row = json.loads(line, parse_constant=invalid_constant)
                    if not isinstance(row, dict):
                        raise ValueError("object required")
                except (ValueError, TypeError):
                    issues.add("invalid_json_object", where)
                    yield None, where
                    continue
                yield row, where
    except (OSError, EOFError, UnicodeError, zlib.error):
        issues.add("unreadable_jsonl", relative)


class UniqueKeys:
    """Точный индекс, включая дубликаты в разных файлах одного kind."""

    def __init__(self, conn, issues):
        self.conn, self.issues = conn, issues
        self.pending = []
        conn.execute("DROP TABLE IF EXISTS audit_keys")
        conn.execute("CREATE TABLE audit_keys (key TEXT PRIMARY KEY) WITHOUT ROWID")

    def add(self, key, where):
        self.pending.append((json.dumps(key, separators=(",", ":")), where))
        if len(self.pending) >= 2000:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        self.conn.execute("SAVEPOINT audit_batch")
        try:
            self.conn.executemany(
                "INSERT INTO audit_keys VALUES (?)", ((key,) for key, _ in self.pending)
            )
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK TO audit_batch")
            for key, where in self.pending:
                try:
                    self.conn.execute("INSERT INTO audit_keys VALUES (?)", (key,))
                except sqlite3.IntegrityError:
                    self.issues.add("duplicate_key", where)
        finally:
            self.conn.execute("RELEASE audit_batch")
            self.pending.clear()


class PackAudit:
    def __init__(self, city, issues, snapshot=None):
        self.city, self.issues, self.snapshot = city, issues, snapshot
        self.parents = defaultdict(set)
        self.room_hotels = {}
        self.hotel_counts = Counter()
        self.feature_hotels = defaultdict(lambda: defaultdict(set))
        self.fact_statuses = Counter()
        self.unknown_reasons = Counter()
        self.checked_subjects = Counter()
        self.linked_hotels = set()
        self.new_passports = 0
        self.twogis_rows = 0
        self.facade_keys = set()
        self.placement_buildings = {}

    def check(self, kind, row, where):
        issue = self.issues.add
        if "source" in row and not isinstance(row["source"], dict):
            raise ValueError("source must be an object")
        if "city_id" in row and row["city_id"] != self.city:
            issue("wrong_city", where)
        if kind in PARENTS:
            self.parents[kind].add(row[KEYS[kind][0]])
        for field, parent in REFERENCES.get(kind, ()):
            if row.get(field) not in self.parents[parent]:
                issue("missing_reference", where, field=field)
        geometry = bool(self.parents["buildings"])  # корпуса идут раньше фасадов и фактов
        if kind in {"hotels", "places", "transit_stations"}:
            if not number(row.get("latitude"), -90, 90) or not number(
                row.get("longitude"), -180, 180
            ):
                issue("invalid_coordinates", where)
        if kind == "hotels":
            if (
                not isinstance(row["id"], str)
                or not row["id"].isdigit()
                or int(row["id"]) < 1
                or str(int(row["id"])) != row["id"]
            ):
                issue("invalid_master_id", where)
            self.hotel_counts[row.get("subzone_id") or row.get("subzone") or "not_specified"] += 1
        elif kind == "room_types":
            if row["id"] != f"{row['hotel_id']}:{row['rg_hash']}":
                issue("invalid_room_type_id", where)
            self.room_hotels[row["id"]] = row["hotel_id"]
        elif kind == "hotel_buildings":
            self.linked_hotels.add(row["hotel_id"])
            if not row.get("method") or not row.get("evidence"):
                issue("building_link_without_evidence", where)
        elif kind == "facades":
            if not number(row.get("azimuth_deg"), 0, 360) or row["azimuth_deg"] == 360:
                issue("invalid_facade_azimuth", where)
            elif row.get("side") != SIDES[int((row["azimuth_deg"] + 22.5) // 45) % 8]:
                issue("facade_side_disagrees_with_azimuth", where)
            if row.get("is_courtyard") is not None and type(row["is_courtyard"]) is not bool:
                issue("invalid_courtyard_flag", where)
            if not number(row.get("length_m")) or row["length_m"] == 0:
                issue("invalid_facade_length", where)
            self.facade_keys.add((row["building_id"], row["facade_id"]))
        elif kind == "places":
            if not row.get("categories"):  # D41: скамейки, урны, парковки в пакет не идут
                issue("place_without_category", where)
        elif kind == "placements":
            self.placement_buildings[row["id"]] = row.get("building_id")
            if geometry and row.get("building_id") not in self.parents["buildings"]:
                issue("missing_reference", where, field="building_id")
        elif kind == "placement_windows":
            building = self.placement_buildings.get(row.get("placement_id"))
            if geometry and (building, row.get("facade_id")) not in self.facade_keys:
                issue("missing_reference", where, field="facade_id")
        elif kind == "hotel_stations":
            if not number(row.get("distance_m"), 0, 30000.001):
                issue("invalid_proximity_distance", where)
            if (
                row.get("distance_kind") == "geodesic_to_representative_point"
                and row.get("walking_distance_m") is not None
            ):
                issue("geodesic_claimed_as_walking", where)
            if type(row.get("rank")) is not int or not 1 <= row["rank"] <= 3:
                issue("invalid_station_rank", where)
        elif kind == "transit_times":
            aware_date(row["departure_at"])
            if (
                not number(row.get("duration_s"))
                or type(row.get("transfers")) is not int
                or row["transfers"] < 0
                or not row.get("profile")
            ):
                issue("invalid_transit_context", where)
        elif kind == "curation_items":
            if (
                row.get("kind") == "match_hotel_building"
                and row.get("subject_id") not in self.parents["hotels"]
            ):
                issue("candidate_without_hotel", where)
            if (
                row.get("state") != "proposed"
                or row.get("reviewer")
                or row.get("reviewed_at")
                or row.get("fact_id")
            ):
                issue("candidate_claims_human_decision", where)
            if not row.get("proposal") or not row.get("evidence"):
                issue("candidate_without_evidence", where)
        elif kind == "facts":
            fact = Fact.model_validate(row)
            self.fact_statuses[fact.status.value] += 1
            if fact.status.value == "unknown":
                self.unknown_reasons[fact.unknown_reason] += 1
            self.feature_hotels[fact.feature_id][fact.hotel_id].add(fact.status.value)
            if fact.subject_level.value == "room_type":
                if self.room_hotels.get(fact.subject_id) != fact.hotel_id:
                    issue("fact_room_owner_mismatch", where)
                self.checked_subjects["room_type"] += 1
            elif fact.subject_level.value == "hotel":
                if fact.subject_id != "hotel:" + fact.hotel_id:
                    issue("fact_hotel_subject_mismatch", where)
                self.checked_subjects["hotel"] += 1
            elif geometry and fact.subject_level.value in GEOMETRY_LEVELS:
                try:
                    building, facade = geometry_subject(fact.subject_level.value, fact.subject_id)
                except ValueError:
                    building, facade = None, None
                if building in self.parents["buildings"] and (
                    facade is None or (building, facade) in self.facade_keys
                ):
                    self.checked_subjects[fact.subject_level.value] += 1
                else:  # и у слоя калькулятора: фасады — те же `seg-…` пакета (D41)
                    issue("fact_geometry_subject_missing", where, field=fact.subject_level.value)
            else:
                # v9 без геометрии в пакете, маршруты: свидетельства вне пакета.
                self.checked_subjects["external_geometry_subject_not_resolved"] += 1
            if fact.status.value in {"unknown", "not_applicable"} and fact.value is not None:
                issue("valueless_status_has_value", where)
            if str(row.get("algorithm_version")).startswith("ostrovok-site."):
                self.new_passports += 1
                if not fact.sources or fact.sources[0].provider != "ostrovok.ru":
                    issue("missing_ostrovok_source", where)
                else:
                    source = row["sources"][0]
                    for field in (
                        "url",
                        "dataset_version",
                        "dataset_sha256",
                        "html_sha256",
                        "json_pointer",
                        "fetched_at",
                        "snapshot_file",
                    ):
                        if not source.get(field):
                            issue("incomplete_ostrovok_source", where, field=field)
                    aware_date(source["fetched_at"])
                    if (
                        fact.subject_level.value == "room_type"
                        and fact.status.value == "ostrovok_content"
                        and fact.hard_condition_eligible is not False
                    ):
                        issue("partial_room_claim_used_as_guarantee", where)
        elif kind == "coverage":
            self.check_coverage(row, where)
        gis = [source for source in passports(row) if source.get("provider") == "2gis"]
        if gis:  # D37: и `source` строк, и каждый паспорт в `sources` фактов
            from connectors.twogis import scrub

            self.twogis_rows += 1
            if scrub(row) != row:
                issue("twogis_forbidden_fields", where)
            for source in gis:
                if source.get("commercial_ok") is not False:
                    issue("twogis_missing_noncommercial_marker", where)
                dates = (
                    [source["fetched_at"]]
                    if source.get("fetched_at")
                    else [page["fetched_at"] for page in source.get("pages", [])]
                )
                if not dates:
                    issue("twogis_retrieval_date_missing", where)
                for date in dates:
                    aware_date(date)
        if self.snapshot:
            self.snapshot.enqueue(kind, row, where)

    def check_coverage(self, row, where):
        expected = Counter()
        for hid in self.parents["hotels"]:
            states = self.feature_hotels[row["feature_id"]].get(hid, set())
            if states & {"computed", "observed", "curated"}:
                expected["computed"] += 1
            elif "estimate" in states:
                expected["estimate"] += 1
            elif states == {"not_applicable"}:
                expected["not_applicable"] += 1
            else:
                expected["unknown"] += 1
        if row.get("n_hotels") != len(self.parents["hotels"]) or any(
            row.get(k, 0) != expected[k]
            for k in ("computed", "estimate", "unknown", "not_applicable")
        ):
            self.issues.add("coverage_disagrees_with_facts", where)


def audit_pack(folder, work_dir=None, snapshot_root=None, progress=None):
    snapshot_roots = (
        list(snapshot_root)
        if isinstance(snapshot_root, list | tuple)
        else ([snapshot_root] if snapshot_root else [])
    )
    if work_dir and any(
        work_dir.resolve().is_relative_to(root.resolve()) for root in [folder, *snapshot_roots]
    ):
        raise ValueError("временный индекс должен находиться вне входных данных")
    started = time.monotonic()
    issues = Issues()
    result = {"pack": folder.name, "status": "failed", "rows": {}, "files": []}
    try:
        safe_path(folder, "manifest.json")
        manifest = read_manifest(folder)
        ZoneInfo(manifest.timezone)
    except (OSError, ValueError, KeyError, TypeError):
        issues.add("invalid_manifest", "manifest.json")
        return result | issues.dump(), set()
    result.update(
        city_id=manifest.city_id,
        pack_version=manifest.pack_version,
        manifest_sha256=sha256_file(folder / "manifest.json"),
    )
    for source in manifest.sources:
        if source.provider == "2gis" and (
            source.commercial_ok is not False
            or (source.fetched_at is not None and source.fetched_at.utcoffset() is None)
        ):
            issues.add("twogis_manifest_provenance_missing", "manifest.json")
    paths, valid_files = set(), []
    for entry in manifest.files:
        try:
            path = safe_path(folder, entry.path)
            if path in paths:
                raise ValueError("duplicate manifest path")
            paths.add(path)
            if entry.rows < 0 or entry.bytes < 0:
                raise ValueError("negative size/count")
            if path.stat().st_size != entry.bytes or sha256_file(path) != entry.sha256:
                issues.add("file_integrity_mismatch", entry.path)
                continue
            valid_files.append((entry, path))
        except (OSError, ValueError):
            issues.add("unsafe_missing_or_repeated_file", entry.path)
    with tempfile.TemporaryDirectory(prefix="seva-audit-", dir=work_dir) as temporary:
        with sqlite3.connect(Path(temporary) / "audit.sqlite") as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA cache_size=-16384")
            snapshot = None
            if snapshot_root:
                from pipeline.audit_ostrovok import SnapshotAudit

                try:
                    snapshot = SnapshotAudit(snapshot_roots, conn, issues)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    issues.add("snapshot_index_unreadable", "manifests/pages.jsonl")
            audit = PackAudit(manifest.city_id, issues, snapshot)
            for kind in ORDER:
                keys = UniqueKeys(conn, issues)
                for entry, path in valid_files:
                    if entry.kind != kind:
                        continue
                    count = 0
                    twogis_file = "_2gis" in Path(entry.path).name
                    for row, where in records(path, entry.path, issues):
                        count += 1
                        if row is None:
                            continue
                        try:
                            key = [row[field] for field in KEYS[kind]]
                            numeric_id = kind in {"questions", "curation_items", "ota_comparisons"}
                            if any(
                                (type(value) is not int or not 0 <= value < 2**63)
                                if numeric_id
                                else (not isinstance(value, str) or not value)
                                for value in key
                            ):
                                raise ValueError("invalid key")
                            keys.add(key, where)
                            if twogis_file != is_twogis(row):
                                issues.add("twogis_file_mixes_sources", where)
                            audit.check(kind, row, where)
                        except (ValueError, TypeError, KeyError, IndexError, ValidationError):
                            issues.add("invalid_record", where)
                    if count != entry.rows:
                        issues.add("row_count_mismatch", entry.path)
                    result["files"].append({"path": entry.path, "rows": count, "kind": kind})
                    result["rows"][kind] = result["rows"].get(kind, 0) + count
                keys.flush()
                conn.commit()
                if progress and result["rows"].get(kind):
                    progress(kind, result["rows"][kind])
            other = [entry.path for entry, _ in valid_files if entry.kind == "other"]
            if other:
                issues.add("other_files_hash_only", "manifest.json", warning=True)
            if snapshot:
                result["snapshot"] = snapshot.validate()
            elif snapshot_root:
                result["snapshot"] = {"status": "failed", "reason": "snapshot_index_unreadable"}
            else:
                result["snapshot"] = {"status": "not_checked", "reason": "snapshot_not_supplied"}
                if audit.new_passports:
                    issues.add("snapshot_not_checked", "manifest.json", warning=True)
            if audit.checked_subjects["external_geometry_subject_not_resolved"]:
                issues.add("external_geometry_subjects_not_resolved", "manifest.json", warning=True)
            if result["rows"].get("transit_stations") and not result["rows"].get("transit_times"):
                issues.add("dated_transit_times_unavailable", "manifest.json", warning=True)
            if audit.twogis_rows and not any(
                source.provider == "2gis" and source.fetched_at is not None
                for source in manifest.sources
            ):
                issues.add("twogis_manifest_provenance_missing", "manifest.json")
            result["fact_statuses"] = dict(audit.fact_statuses)
            result["unknown_reasons"] = dict(audit.unknown_reasons)
            result["fact_subject_checks"] = dict(audit.checked_subjects)
            result["ostrovok_import_passports"] = audit.new_passports
            result["subzones"] = dict(audit.hotel_counts)
            result["building_linked_hotels"] = len(audit.linked_hotels)
            hotels = set(audit.parents["hotels"])
    result.update(
        status="passed" if not issues.errors else "failed",
        seconds=round(time.monotonic() - started, 3),
    )
    return result | issues.dump(), hotels


def main(argv=None):
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packs", type=Path, default=Path("packs"))
    parser.add_argument("--city", action="append", default=[])
    parser.add_argument("--snapshot", type=Path, action="append", default=[])
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    folders = (
        [args.packs]
        if (args.packs / "manifest.json").is_file()
        else sorted(path.parent for path in args.packs.glob("*/manifest.json"))
    )
    if args.city:
        folders = [folder for folder in folders if folder.name in args.city]
    if not folders or set(args.city) - {folder.name for folder in folders}:
        parser.error("пакеты выбранных городов не найдены")
    roots = [args.packs] + args.snapshot
    if any(args.report.resolve().is_relative_to(root.resolve()) for root in roots):
        parser.error("отчёт должен находиться вне входных пакетов и снимка")
    if args.work_dir and any(
        args.work_dir.resolve().is_relative_to(root.resolve()) for root in roots
    ):
        parser.error("временный индекс должен находиться вне входных пакетов и снимка")
    if any(not (root / "manifests/pages.jsonl").is_file() for root in args.snapshot):
        parser.error("нет журнала указанного снимка")
    if args.work_dir and not args.work_dir.is_dir():
        parser.error("рабочий каталог для временного индекса должен существовать")
    report = {
        "version": VERSION,
        "built_at": datetime.now(UTC).isoformat(),
        "cities": [],
        "duplicate_hotels_between_cities": [],
    }
    seen = set()
    for folder in folders:
        print(f"audit: {folder.name}", flush=True)
        result, hotels = audit_pack(
            folder,
            args.work_dir,
            args.snapshot,
            progress=lambda kind, count, city=folder.name: print(
                f"audit: {city}: {kind}: {count}", flush=True
            ),
        )
        report["cities"].append(result)
        report["duplicate_hotels_between_cities"].extend(sorted(seen & hotels))
        seen.update(hotels)
        print(f"audit: {folder.name}: {result['status']}", flush=True)
    report["status"] = (
        "passed"
        if all(city["status"] == "passed" for city in report["cities"])
        and not report["duplicate_hotels_between_cities"]
        else "failed"
    )
    report["seconds"] = round(time.monotonic() - started, 3)
    report["code_sha256"] = {
        name: sha256_file(Path(__file__).with_name(name))
        for name in ("audit_packs.py", "audit_ostrovok.py")
    }
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["peak_rss_mib"] = round(rss / (1024**2 if sys.platform == "darwin" else 1024), 2)
    except ImportError:
        report["peak_rss_mib"] = None
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
