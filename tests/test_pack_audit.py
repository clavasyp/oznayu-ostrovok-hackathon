import copy
import gzip
import json
import shutil
from datetime import UTC, datetime

import pytest

from core.packs import Manifest, PackFile, sha256_file
from pipeline.audit_ostrovok import resolve_pointer
from pipeline.audit_packs import audit_pack, is_twogis, main
from tests.test_ostrovok_import import snapshot as snapshot


def write_pack(root, parts, *, split_links=False, gis_files=False, layers=()):
    """Ключ `parts` — вид файла; `facts@view_sun` — второй файл того же вида (слой из `layers`)."""
    root.mkdir(parents=True)
    files = []
    layer_paths = set()
    for key, rows in parts.items():
        kind = key.split("@")[0]
        groups = [[row] for row in rows] if split_links and kind == "hotel_stations" else [rows]
        for index, group in enumerate(groups):
            suffix = "_2gis" if gis_files and group and is_twogis(group[0]) else ""
            name = f"{key.replace('@', '_')}{suffix}-{index:04d}.jsonl.gz"
            if key in layers:
                layer_paths.add(name)
            path = root / name
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in group)
            files.append(
                PackFile(
                    path=name,
                    kind=kind,
                    rows=len(group),
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
    manifest = Manifest(
        city_id="moscow",
        city_name="Москва",
        timezone="Europe/Moscow",
        depth="full",
        pack_version="test",
        built_at=datetime(2026, 10, 26, tzinfo=UTC),
        files=files,
    )
    raw = json.loads(manifest.model_dump_json())
    for entry in raw["files"]:
        if entry["path"] in layer_paths:  # файл-слой калькулятора, как `core.packs.add_layer`
            entry["layer"] = True
    (root / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    return root


@pytest.fixture
def parts():
    return {
        "hotels": [
            {
                "id": "101",
                "city_id": "moscow",
                "name": "Synthetic",
                "latitude": 55.7,
                "longitude": 37.6,
            }
        ],
        "room_types": [{"id": "101:r", "hotel_id": "101", "rg_hash": "r"}],
        "places": [
            {
                "id": "osm:node/1",
                "city_id": "moscow",
                "latitude": 55.7,
                "longitude": 37.6,
                "categories": ["L01"],
            }
        ],
        "transit_stations": [
            {
                "id": "osm:node/9",
                "city_id": "moscow",
                "latitude": 55.7,
                "longitude": 37.6,
                "mode": "metro",
            }
        ],
        "hotel_stations": [
            {
                "hotel_id": "101",
                "station_id": "osm:node/9",
                "rank": 1,
                "distance_m": 0,
                "distance_kind": "geodesic_to_representative_point",
                "walking_distance_m": None,
            }
        ],
        "facts": [
            {
                "fact_id": "a",
                "hotel_id": "101",
                "city_id": "moscow",
                "subject_level": "room_type",
                "subject_id": "101:r",
                "feature_id": "M01",
                "status": "unknown",
                "unknown_reason": "not_available",
            }
        ],
        "coverage": [
            {
                "city_id": "moscow",
                "feature_id": "M01",
                "n_hotels": 1,
                "computed": 0,
                "estimate": 0,
                "unknown": 1,
                "not_applicable": 0,
            }
        ],
    }


def test_audit_reads_valid_chunks_without_changing_inputs(tmp_path, parts):
    pack = write_pack(tmp_path / "pack", parts)
    before = {path.name: sha256_file(path) for path in pack.iterdir()}
    report, hotels = audit_pack(pack, work_dir=tmp_path)
    assert report["status"] == "passed" and report["errors"] == {}
    assert hotels == {"101"} and report["rows"]["facts"] == 1
    assert report["snapshot"]["status"] == "not_checked"
    assert before == {path.name: sha256_file(path) for path in pack.iterdir()}
    assert not list(tmp_path.glob("seva-audit-*"))


def test_duplicates_between_separate_files_and_foreign_keys_are_detected(tmp_path, parts):
    parts["hotel_stations"] *= 2
    parts["hotel_stations"].append(parts["hotel_stations"][0] | {"station_id": "missing"})
    pack = write_pack(tmp_path / "pack", parts, split_links=True)
    report, _ = audit_pack(pack)
    assert report["errors"]["duplicate_key"] == 1
    assert report["errors"]["missing_reference"] == 1
    assert report["rows"]["hotel_stations"] == 3


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda p: p["hotels"][0].update(city_id="sochi"), "wrong_city"),
        (lambda p: p["hotels"][0].update(latitude=True), "invalid_coordinates"),
        (lambda p: p["facts"][0].update(subject_id="another:r"), "fact_room_owner_mismatch"),
        (lambda p: p["facts"][0].update(unknown_reason=None), "invalid_record"),
        (lambda p: p["hotel_stations"][0].update(distance_m=30001), "invalid_proximity_distance"),
        (
            lambda p: p["hotel_stations"][0].update(walking_distance_m=0),
            "geodesic_claimed_as_walking",
        ),
        (lambda p: p["places"][0].update(categories=[]), "place_without_category"),
        (lambda p: p["coverage"][0].update(unknown=0), "coverage_disagrees_with_facts"),
        (lambda p: p["hotels"][0].update(source=[]), "invalid_record"),
    ],
)
def test_invalid_data_is_reported_instead_of_loaded(tmp_path, parts, mutation, code):
    mutation(parts)
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    assert report["status"] == "failed" and report["errors"][code]


def test_bad_hash_and_wrong_row_count_are_separate_errors(tmp_path, parts):
    pack = write_pack(tmp_path / "pack", parts)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["sha256"] = "0" * 64
    manifest["files"][-1]["rows"] += 1
    path.write_text(json.dumps(manifest))
    report, _ = audit_pack(pack)
    assert report["errors"]["file_integrity_mismatch"] == 1
    assert report["errors"]["row_count_mismatch"] == 1


@pytest.mark.parametrize("escape", ["../outside.jsonl.gz", "/etc/passwd", "escape.jsonl.gz"])
def test_manifest_cannot_read_outside_the_pack(tmp_path, parts, escape):
    pack = write_pack(tmp_path / "pack", parts)
    outside = tmp_path / "outside.jsonl.gz"
    outside.write_bytes(b"not package input")
    (pack / "escape.jsonl.gz").symlink_to(outside)
    path = pack / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = escape
    path.write_text(json.dumps(manifest))
    report, _ = audit_pack(pack)
    assert report["errors"]["unsafe_missing_or_repeated_file"] == 1


def test_nonfinite_json_and_malformed_records_do_not_crash(tmp_path, parts):
    parts["hotels"].extend([{"id": "102", "latitude": float("nan")}, None, []])
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    assert report["errors"]["invalid_json_object"] == 3


def test_report_and_temporary_index_cannot_overwrite_inputs(tmp_path, parts):
    pack = write_pack(tmp_path / "pack", parts)
    before = (pack / "manifest.json").read_bytes()
    with pytest.raises(SystemExit):
        main(["--packs", str(pack), "--report", str(pack / "manifest.json")])
    with pytest.raises(ValueError):
        audit_pack(pack, work_dir=pack)
    assert (pack / "manifest.json").read_bytes() == before


def test_cli_returns_failure_and_writes_json_report(tmp_path, parts):
    parts["hotel_stations"][0]["station_id"] = "missing"
    pack = write_pack(tmp_path / "pack", parts)
    target = tmp_path / "report.json"
    assert main(["--packs", str(pack), "--report", str(target)]) == 1
    assert json.loads(target.read_text())["status"] == "failed"


def snapshot_parts(snapshot):
    from connectors.ostrovok_site import parse_page

    root, add = snapshot
    entry, _ = add()
    page = parse_page(root, entry)
    return root, {
        "hotels": [h.model_dump(mode="json") for h in page.hotels],
        "room_types": [r.model_dump(mode="json") for r in page.room_types],
        "facts": [f.model_dump(mode="json") for f in page.facts],
    }


def test_snapshot_json_hash_pointer_values_and_completeness(snapshot, tmp_path):
    root, parts = snapshot_parts(snapshot)
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=root)
    assert report["status"] == "passed"
    assert report["snapshot"]["json_files"] == 1
    assert report["snapshot"]["records"]["facts"] == len(parts["facts"])
    assert report["snapshot"]["html_verification"] == "hash_compared_to_index_only"


@pytest.mark.parametrize(
    "change", ["value", "gzip_hash", "pointer", "date", "missing_fact", "missing_room", "guarantee"]
)
def test_source_disagreements_are_caught_even_with_valid_package_hashes(snapshot, tmp_path, change):
    root, parts = snapshot_parts(snapshot)
    row = next(f for f in parts["facts"] if f["status"] == "ostrovok_content")
    if change == "value":
        row["value"] = {"invented": True}
    elif change == "gzip_hash":
        row["sources"][0]["dataset_sha256"] = sha256_file(root / row["sources"][0]["snapshot_file"])
    elif change == "pointer":
        row["sources"][0]["json_pointer"] = "/props/pageProps/hotel/roomGroups/-1"
    elif change == "date":
        row["sources"][0]["fetched_at"] = "2026-10-25T16:00:00Z"
    elif change == "missing_fact":
        parts["facts"].remove(row)
    elif change == "missing_room":
        parts["room_types"].pop()
    else:
        row["hard_condition_eligible"] = True
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=root)
    assert report["status"] == "failed" and report["snapshot"]["status"] == "failed"


@pytest.mark.parametrize("pointer", ["/a/-1", "/a/01", "/a/0/~", "/a/0/~2"])
def test_json_pointer_rejects_nonstandard_indices_and_escapes(pointer):
    with pytest.raises((ValueError, KeyError)):
        resolve_pointer({"a": [{"~": 1}]}, pointer)


def test_json_pointer_escaped_keys_and_root():
    document = {"a/b": {"~key": [7]}}
    assert resolve_pointer(document, "/a~1b/~0key/0") == 7
    assert resolve_pointer(document, "") is document


def test_facade_and_proposed_review_seed_rules(tmp_path, parts):
    parts = copy.deepcopy(parts)
    parts["buildings"] = [{"id": "way/1", "city_id": "moscow"}]
    parts["facades"] = [
        {
            "building_id": "way/1",
            "facade_id": "west",
            "azimuth_deg": 270,
            "side": "E",
            "length_m": 4,
            "is_courtyard": "yes",
        }
    ]
    parts["curation_items"] = [
        {
            "id": 1,
            "kind": "match_hotel_building",
            "subject_id": "101",
            "state": "accepted",
            "reviewer": "invented",
            "proposal": {"building_id": "way/1"},
            "evidence": [{}],
        }
    ]
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    for code in (
        "facade_side_disagrees_with_azimuth",
        "invalid_courtyard_flag",
        "candidate_claims_human_decision",
    ):
        assert report["errors"][code] == 1


def test_duplicate_after_batch_boundary_does_not_lose_other_keys(tmp_path, parts):
    parts["places"] = [parts["places"][0] | {"id": f"osm:node/{i}"} for i in range(2100)]
    parts["places"].append(parts["places"][10])
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    assert report["errors"] == {"duplicate_key": 1}
    assert report["rows"]["places"] == 2101


def test_invalid_snapshot_index_yields_report_instead_of_traceback(tmp_path, parts):
    root = tmp_path / "snapshot"
    (root / "manifests").mkdir(parents=True)
    (root / "manifests/pages.jsonl").write_text("not JSON\n")
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=root)
    assert report["errors"]["snapshot_index_unreadable"] == 1
    assert report["snapshot"]["status"] == "failed"


@pytest.mark.parametrize("change", [None, "mixed_file", "missing_date", "reviews"])
def test_twogis_page_passports(tmp_path, parts, change):
    stamp = "2026-10-26T10:00:00+00:00"
    source = {"provider": "2gis", "commercial_ok": False, "pages": [{"fetched_at": stamp}]}
    parts["places"][0]["source"] = source
    if change == "missing_date":
        source.pop("pages")
    if change == "reviews":
        parts["places"][0]["reviews"] = [{"text": "do not store"}]
    pack = write_pack(tmp_path / "pack", parts, gis_files=change != "mixed_file")
    manifest = json.loads((pack / "manifest.json").read_text())
    manifest["sources"] = [
        {"id": "2gis", "provider": "2gis", "commercial_ok": False, "fetched_at": stamp}
    ]
    (pack / "manifest.json").write_text(json.dumps(manifest))
    report, _ = audit_pack(pack)
    if change is None:
        assert report["status"] == "passed", report["errors"]
    else:
        code = {
            "mixed_file": "twogis_file_mixes_sources",
            "missing_date": "twogis_retrieval_date_missing",
            "reviews": "twogis_forbidden_fields",
        }[change]
        assert report["status"] == "failed" and report["errors"][code]


def two_source_parts(snapshot, tmp_path):
    from connectors.ostrovok_site import parse_page

    root, add = snapshot
    original, _ = add(hid="101")
    supplement, _ = add(hid="202")
    other = tmp_path / "snapshot_extra"
    shutil.copytree(root, other)
    old = parse_page(root, original)
    new = parse_page(other, supplement)
    parts = {
        "hotels": [
            old.hotels[0].model_dump(mode="json"),
            new.hotels[0].model_dump(mode="json") | {"source": {"legacy_v9_only": True}},
        ],
        "room_types": [r.model_dump(mode="json") for r in old.room_types],
        "facts": [f.model_dump(mode="json") for f in old.facts],
    }
    for room in new.room_types:
        row = room.model_dump(mode="json")
        row["source"]["import_scope"] = "room_categories_only"
        parts["room_types"].append(row)
    for fact in new.facts:
        row = fact.model_dump(mode="json")
        row["sources"][0]["import_scope"] = "room_categories_only"
        parts["facts"].append(row)
    return root, other, parts


def test_multiple_dated_snapshots_and_category_only_scope(snapshot, tmp_path):
    root, other, parts = two_source_parts(snapshot, tmp_path)
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=[root, other])
    assert report["status"] == "passed", report["errors"]
    assert report["snapshot"]["json_files"] == 2
    assert report["snapshot"]["category_only_pages"] == 1
    assert set(report["snapshot"]["index_sha256_by_dataset"]) == {root.name, other.name}


def test_missing_snapshot_cannot_be_reported_as_verified(snapshot, tmp_path):
    root, _, parts = two_source_parts(snapshot, tmp_path)
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=root)
    assert report["status"] == "failed"
    assert report["errors"]["snapshot_dataset_not_supplied"] > 0
    assert report["snapshot"]["unverified_references"] > 0


def test_unknown_scope_cannot_skip_source_validation(snapshot, tmp_path):
    root, other, parts = two_source_parts(snapshot, tmp_path)
    parts["room_types"][-1]["source"]["import_scope"] = "skip_all_checks"
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts), snapshot_root=[root, other])
    assert report["status"] == "failed" and report["errors"]["snapshot_record_mismatch"]


def geometry_parts(parts):
    """Пакет с корпусом, фасадом, размещением, окном и фактом окна (D17, D39)."""
    parts = copy.deepcopy(parts)
    parts["buildings"] = [{"id": "way/1", "city_id": "moscow"}]
    parts["facades"] = [
        {
            "building_id": "way/1",
            "facade_id": "seg-a",
            "azimuth_deg": 270,
            "side": "W",
            "length_m": 10,
            "is_courtyard": None,
        }
    ]
    parts["placement_sets"] = [{"room_type_id": "101:r"}]
    parts["placements"] = [
        {"id": "101:r|way/1|3", "room_type_id": "101:r", "building_id": "way/1", "floor": 3}
    ]
    parts["placement_windows"] = [{"placement_id": "101:r|way/1|3", "facade_id": "seg-a"}]
    parts["facts"].append(
        {
            "fact_id": "w",
            "hotel_id": "101",
            "city_id": "moscow",
            "subject_level": "facade_physical_floor",
            "subject_id": "way/1:seg-a:floor3",
            "feature_id": "V05",
            "status": "unknown",
            "unknown_reason": "test",
        }
    )
    return parts


@pytest.mark.parametrize(
    "mutation,code,count",
    [
        # Размещение на чужом корпусе — ошибка и у него, и у его окна.
        (lambda p: p["placements"][0].update(building_id="way/404"), "missing_reference", 2),
        (
            lambda p: p["placement_windows"][0].update(facade_id="seg-missing"),
            "missing_reference",
            1,
        ),
        (
            lambda p: p["facts"][-1].update(subject_id="way/999:does-not-exist:floor3"),
            "fact_geometry_subject_missing",
            1,
        ),
        # Схема id фасадов v9 (`p0:r0:e0`) не совпадает с `seg-…` пакета.
        (
            lambda p: p["facts"][-1].update(subject_id="way/1:p0:r0:e0:floor3"),
            "fact_geometry_subject_missing",
            1,
        ),
        (
            lambda p: p["facts"][-1].update(subject_level="building", subject_id="way/2"),
            "fact_geometry_subject_missing",
            1,
        ),
    ],
)
def test_building_and_facade_references_are_checked_when_pack_has_geometry(
    tmp_path, parts, mutation, code, count
):
    parts = geometry_parts(parts)
    report, _ = audit_pack(write_pack(tmp_path / "ok", parts))
    assert report["status"] == "passed", report["errors"]
    assert report["fact_subject_checks"]["facade_physical_floor"] == 1
    mutation(parts)
    report, _ = audit_pack(write_pack(tmp_path / "broken", parts))
    assert report["status"] == "failed"
    assert report["errors"] == {code: count}


def test_layer_file_with_unknown_facade_is_an_error(tmp_path, parts):
    """Слой «вид и солнце» считается на фасадах пакета (`seg-…`, D41): окно на фасаде, которого
    в пакете нет, — ошибка и в слое, и в основном файле фактов."""
    parts = geometry_parts(parts)
    window = parts["facts"].pop()
    parts["facts@view_sun"] = [window | {"subject_id": "way/1:p0:r0:e0:floor3"}]
    report, _ = audit_pack(write_pack(tmp_path / "layer", parts, layers=("facts@view_sun",)))
    assert report["status"] == "failed" and report["errors"]["fact_geometry_subject_missing"] == 1
    report, _ = audit_pack(write_pack(tmp_path / "plain", parts))
    assert report["errors"]["fact_geometry_subject_missing"] == 1


def test_pack_without_buildings_keeps_v9_subjects_as_warning(tmp_path, parts):
    parts["facts"].append(geometry_parts(parts)["facts"][-1])
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    assert report["status"] == "passed", report["errors"]
    assert report["warnings"]["external_geometry_subjects_not_resolved"] == 1


def test_empty_ostrovok_sources_with_snapshot_give_report_not_traceback(snapshot, tmp_path):
    root, parts = snapshot_parts(snapshot)
    row = next(f for f in parts["facts"] if f["status"] == "ostrovok_content")
    row["sources"] = []
    pack = write_pack(tmp_path / "pack", parts)
    report, _ = audit_pack(pack, snapshot_root=root)
    assert report["status"] == "failed" and report["errors"]["missing_ostrovok_source"] == 1
    target = tmp_path / "report.json"
    assert main(["--packs", str(pack), "--snapshot", str(root), "--report", str(target)]) == 1
    assert json.loads(target.read_text())["status"] == "failed"


def test_twogis_passport_in_fact_sources_is_checked(tmp_path, parts):
    """D37: паспорт 2ГИС в списке `sources` факта проверяется так же, как `source` строки."""
    parts["facts"][0]["sources"] = [
        {
            "provider": "2gis",
            "commercial_ok": True,
            "fetched_at": "2026-10-26T10:00:00+00:00",
            "reviews": [{"text": "do not store"}],
        }
    ]
    report, _ = audit_pack(write_pack(tmp_path / "pack", parts))
    for code in (
        "twogis_missing_noncommercial_marker",
        "twogis_forbidden_fields",
        "twogis_file_mixes_sources",
    ):
        assert report["errors"][code] == 1, report["errors"]


def test_committed_audit_report_matches_current_packs():
    """Отчёт в репозитории — по текущим манифестам. Пакет изменился — `make audit`."""
    from pathlib import Path

    from core.packs import discover

    root = Path(__file__).resolve().parents[1]
    report = json.loads((root / "pipeline/reports/seva_pack_quality_audit.json").read_text())
    audited = {c["city_id"]: c for c in report["cities"]}
    for pack in discover(root / "packs"):
        manifest = Manifest.model_validate_json((pack / "manifest.json").read_text("utf-8"))
        city = audited.get(manifest.city_id)
        assert city, f"{manifest.city_id}: нет в отчёте аудита — make audit"
        assert city["manifest_sha256"] == sha256_file(pack / "manifest.json"), (
            f"{manifest.city_id}: пакет изменился после аудита — make audit и закоммитить отчёт"
        )
        assert city["status"] == "passed", f"{manifest.city_id}: аудит не прошёл"


def test_coverage_is_recomputed_when_a_facts_layer_is_added(tmp_path, parts):
    """Слой вида и солнца (`add_layer`) пересчитывает покрытие: аудит не видит расхождения."""
    from core.packs import PackSource, add_layer
    from pipeline.build_v9_packs import write_jsonl_gz

    pack = write_pack(tmp_path / "pack", parts)
    layer = {
        "fact_id": "layer-1",
        "hotel_id": "101",
        "city_id": "moscow",
        "subject_level": "hotel",
        "subject_id": "hotel:101",
        "feature_id": "M01",
        "status": "computed",
        "value": {"bed_configuration": "double"},
    }
    write_jsonl_gz(pack / "layer.jsonl.gz", [layer])
    manifest = add_layer(pack, "layer.jsonl.gz", "facts", 1, [PackSource(id="l:s", provider="p")])
    (coverage,) = [f for f in manifest.files if f.kind == "coverage"]
    (row,) = gzip.open(pack / coverage.path, "rt").read().splitlines()
    assert json.loads(row)["computed"] == 1 and json.loads(row)["unknown"] == 0
    report, _ = audit_pack(pack)
    assert "coverage_disagrees_with_facts" not in report["errors"]
