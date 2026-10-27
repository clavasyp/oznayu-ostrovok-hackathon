from datetime import UTC, datetime

import pytest

from connectors.ostrovok_site import parse_page
from core.packs import iter_jsonl, verify
from core.placement_catalog import placement_choices, placement_maps
from pipeline.build_packs import write_pack
from pipeline.supplement_categories import supplement
from tests.test_ostrovok_import import snapshot as snapshot


def base_pack(tmp_path, page, *, with_rooms=False):
    base = tmp_path / "base"
    parts = {
        "hotels": [page.hotels[0].model_dump(mode="json")],
        "room_types": [r.model_dump(mode="json") for r in page.room_types] if with_rooms else [],
        "facts": [],
    }
    write_pack(
        base,
        "moscow",
        parts,
        sources=[],
        built_at=datetime(2026, 10, 26, tzinfo=UTC),
        code_version="test",
        notes="synthetic",
    )
    return base


def test_supplement_adds_real_ids_and_preserves_old_files(snapshot, tmp_path):
    root, add = snapshot
    entry, _ = add()
    page = parse_page(root, entry)
    base = base_pack(tmp_path, page)
    previous = verify(base / "moscow")
    report = supplement(base, root, tmp_path / "out", ["101"])
    assert report["hotels"]["101"]["added_categories"] == 2
    assert report["hotels"]["101"]["status"] == "added"
    result = verify(tmp_path / "out/moscow")
    for entry in previous.files:
        assert next(f.sha256 for f in result.files if f.path == entry.path) == entry.sha256
    added = [
        r
        for f in result.files
        if f.kind == "room_types"
        for r in iter_jsonl(tmp_path / "out/moscow" / f.path)
    ]
    assert {r["id"] for r in added} == {"101:rg1", "101:rg2"}
    assert all(r["source"]["import_scope"] == "room_categories_only" for r in added)


def test_existing_categories_are_not_replaced(snapshot, tmp_path):
    root, add = snapshot
    entry, _ = add()
    base = base_pack(tmp_path, parse_page(root, entry), with_rooms=True)
    report = supplement(base, root, tmp_path / "out", ["101"])
    assert report["hotels"]["101"]["status"] == "already_present"
    assert not report["cities"] and not (tmp_path / "out/moscow").exists()


def test_empty_room_groups_remain_unknown(snapshot, tmp_path):
    root, add = snapshot
    entry, _ = add(mutate=lambda h: h.update(roomGroups=[]))
    base = base_pack(tmp_path, parse_page(root, entry))
    report = supplement(base, root, tmp_path / "out", ["101"])
    assert report["hotels"]["101"]["status"] == "room_groups_empty"
    assert report["hotels"]["101"]["added_categories"] == 0


def test_relocated_hotel_requires_review(snapshot, tmp_path):
    root, add = snapshot
    entry, _ = add()
    page = parse_page(root, entry)
    page.hotels[0].latitude += 0.01
    base = base_pack(tmp_path, page)
    with pytest.raises(ValueError, match="50m"):
        supplement(base, root, tmp_path / "out", ["101"])


def test_building_choices_expose_ambiguity_without_inventing_aliases():
    links = [{"hotel_id": "h", "building_id": "way/1", "evidence": {}}]
    buildings = [
        {
            "id": "way/1",
            "tags": {
                "addr:street": "Тестовая",
                "addr:housenumber": "1Б",
                "building:levels": "9",
                "name": "Mapped name",
            },
        }
    ]
    facades = [
        {"building_id": "way/1", "facade_id": fid, "side": side, "is_courtyard": None}
        for fid, side in (("sw1", "SW"), ("sw2", "SW"), ("n", "N"))
    ]
    (choice,) = placement_choices(links, buildings, facades, "h")
    assert choice["mapped_address"] == "Тестовая, 1Б" and choice["mapped_levels"] == 9
    assert choice["hotel_aliases"] == [] and choice["category_floors_verified"] is False
    assert choice["ambiguous_sides"][0]["facade_ids"] == ["sw1", "sw2"]
    assert choice["unambiguous_facades"] == [{"side": "N", "facade_id": "n"}]
    assert ("way/1", "SW") not in placement_maps(links, facades, "h")[1]


def test_supplement_fixes_city_page_link_and_question_prefill(snapshot, tmp_path):
    root, add = snapshot
    entry, _ = add(
        hid="999",
        mutate=lambda h: h["roomGroups"][0]["nameStruct"].update(beddingType="2 отдельные кровати"),
    )
    page = parse_page(root, entry)
    base = tmp_path / "base"
    hotel = page.hotels[0].model_dump(mode="json")
    hotel["source"] = {"url": "https://ostrovok.ru/hotel/russia/moscow/", "ota_slug": "current"}
    question = {
        "id": 7, "hotel_id": "999", "feature_id": "M01", "subject_level": "hotel",
        "subject_id": "hotel:999", "text": "?", "prefill": [], "fields": [], "priority": 0.0,
        "reason": "unknown", "state": "not_asked", "answer_text": None, "is_simulated": False,
    }  # fmt: skip
    write_pack(
        base, "moscow", {"hotels": [hotel], "room_types": [], "facts": [], "questions": [question]},
        sources=[], built_at=datetime(2026, 10, 26, tzinfo=UTC), code_version="test",
        notes="synthetic",
    )  # fmt: skip
    report = supplement(base, root, tmp_path / "out", ["999"])
    assert report["hotels"]["999"]["url_updated"] == entry["final_url"]
    out = tmp_path / "out/moscow"
    manifest = verify(out)
    (row,) = [h for f in manifest.files if f.kind == "hotels" for h in iter_jsonl(out / f.path)]
    assert row["source"]["url"] == entry["final_url"]
    assert row["source"]["url_previous"] == "https://ostrovok.ru/hotel/russia/moscow/"
    assert row["source"]["url_from"] == root.name
    assert (row["latitude"], row["name"]) == (hotel["latitude"], hotel["name"])
    (q,) = [r for f in manifest.files if f.kind == "questions" for r in iter_jsonl(out / f.path)]
    assert [c["quote"] for c in q["prefill"]] == ["2 отдельные кровати"]
