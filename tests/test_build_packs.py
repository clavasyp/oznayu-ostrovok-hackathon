import gzip

import pytest

from core.packs import discover, iter_jsonl, verify
from pipeline.build_v9_packs import PACKS, ota_row, question_row, write_jsonl_gz


def test_committed_packs_are_intact():
    packs = discover(PACKS)
    assert {p.name for p in packs} >= {"sochi", "moscow"}
    for pack in packs:
        manifest = verify(pack)  # размеры и sha256 файлов совпадают с manifest.json
        kinds = {f.kind for f in manifest.files}
        assert {"hotels", "facts"} <= kinds
        assert manifest.depth == ("full" if pack.name in {"sochi", "moscow"} else "automatic")


def test_pack_hotels_cover_pack_facts():
    pack = PACKS / "sochi"
    hotels = {h["id"] for h in iter_jsonl(pack / "data" / "hotels.jsonl.gz")}
    facts = {f["hotel_id"] for f in iter_jsonl(pack / "data" / "facts.jsonl.gz")}
    assert facts <= hotels


def test_gzip_is_deterministic(tmp_path):
    rows = [{"b": 2, "a": "й"}, {"a": 1}]
    write_jsonl_gz(tmp_path / "1.gz", rows)
    write_jsonl_gz(tmp_path / "2.gz", rows)
    assert (tmp_path / "1.gz").read_bytes() == (tmp_path / "2.gz").read_bytes()
    lines = gzip.decompress((tmp_path / "1.gz").read_bytes()).decode().splitlines()
    assert lines == ['{"a": "й", "b": 2}', '{"a": 1}']


def test_question_and_comparison_ids_are_stable():
    q = {
        "hotel_id": "1",
        "city_id": "sochi",
        "feature_id": "S04",
        "question": "Есть ли кондиционер?",
        "parsed_fields": ["hvac_type"],
        "answer_text": None,
        "is_simulated": False,
        "answer_status": "not_asked",
        "claims": [],
    }
    assert question_row(q)["id"] == question_row(dict(q))["id"]
    assert question_row(q)["state"] == "not_asked"
    r = {
        "city_id": "sochi",
        "hotel_id": "1",
        "feature_id": "H01",
        "ota_value": "2021",
        "other_value": "{}",
        "classification": "x",
        "numeric_comparison": "",
        "resolution": "",
        "ota_url": "u",
        "ota_pointer": "/p",
    }
    row = ota_row(r)
    assert row["other_value"] == {} and row["numeric_comparison"] is None
    with pytest.raises(KeyError):
        ota_row({})


def test_packs_have_no_files_outside_manifest_and_focus_cities_are_not_v9():
    """Сироты вне манифеста (например, прежние hotel_places) в packs/ не остаются (D41)."""
    for pack in discover(PACKS):
        manifest = verify(pack)
        on_disk = {str(p.relative_to(pack)) for p in (pack / "data").rglob("*") if p.is_file()}
        assert on_disk == {f.path for f in manifest.files}, pack.name
        assert not any("hotel_places" in path for path in on_disk)
        v9 = (manifest.code_version or "").startswith("v9-packs")
        assert v9 == (pack.name not in {"moscow", "sochi"}), pack.name


def test_v9_builder_refuses_to_overwrite_full_catalog(tmp_path):
    import shutil

    from core.packs import PackError
    from pipeline.build_v9_packs import build

    (tmp_path / "sochi").mkdir()
    shutil.copy(PACKS / "sochi" / "manifest.json", tmp_path / "sochi" / "manifest.json")
    with pytest.raises(PackError, match="другой сборки"):
        build(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sochi"]  # ничего не записано
    assert build(tmp_path, ["kazan"])[0].startswith("kazan:")  # автоматический город — можно


def test_catalog_builder_refuses_full_catalog_as_v9_base(tmp_path):
    from pipeline.build_packs import build_catalog

    with pytest.raises(ValueError, match="нужен v9"):
        build_catalog(tmp_path / "snapshot", tmp_path / "osm", tmp_path / "out", v9_packs=PACKS)


def test_v9_link_without_mid_is_derived_and_marked():
    from pipeline.build_packs import derived_page_url

    city_page = {"url": "https://ostrovok.ru/hotel/russia/adler/", "ota_slug": "mio_apartments"}
    fixed = derived_page_url("9769189", city_page)
    assert fixed["url"] == "https://ostrovok.ru/hotel/russia/adler/mid9769189/mio_apartments/"
    assert fixed["url_previous"] == city_page["url"]
    assert fixed["url_method"] == "derived_from_masterId_unverified"
    hotel_page = {"url": "https://ostrovok.ru/hotel/russia/sochi/mid1/x/", "ota_slug": "x"}
    assert derived_page_url("1", hotel_page) == hotel_page
    assert derived_page_url("1", {"url": city_page["url"]}) == {"url": city_page["url"]}


def test_pack_parts_skip_layer_files(tmp_path):
    """Слой вида и солнца — отдельная сборка: пересборка не вливает его в общий `facts`."""
    from core.packs import Manifest, PackFile, sha256_file, write_manifest
    from pipeline.build_packs import pack_parts

    pack = tmp_path / "sochi"
    (pack / "data").mkdir(parents=True)
    files = []
    for name, layer in (("facts", None), ("view_sun", True)):
        path = pack / "data" / f"{name}.jsonl.gz"
        write_jsonl_gz(path, [{"fact_id": name}])
        files.append(
            PackFile(
                path=f"data/{name}.jsonl.gz",
                kind="facts",
                rows=1,
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
                layer=layer,
            )  # fmt: skip
        )
    write_manifest(
        pack,
        Manifest(
            city_id="sochi",
            city_name="Сочи",
            timezone="Europe/Moscow",
            depth="full",
            pack_version="x",
            built_at="2026-10-27T00:00:00Z",
            files=files,
        ),  # fmt: skip
    )
    assert pack_parts(pack)["facts"] == [{"fact_id": "facts"}]


def hotel(hid, name, address, lat=43.5, lon=39.9):
    url = f"https://ostrovok.ru/hotel/russia/sochi/mid{hid}/x/"
    return {"id": hid, "city_id": "sochi", "name": name, "address": address, "latitude": lat,
            "longitude": lon, "source": {"url": url}}  # fmt: skip


def test_duplicate_listings_become_curation_candidates_not_merges():
    from pipeline.build_packs import validate_parts
    from pipeline.building_review import duplicate_listings

    hotels = [
        hotel("8661518", "Mini-hotel Maro", "8 Marta str., 1, Сочи"),
        hotel("8747284", "Mini-hotel Maro", "Russia, Sochi, 8 Marta Str. 1, Сочи", lat=43.50001),
        # квартиры одного оператора в одной точке карты, но в разных домах — не дубли
        hotel("8684592", "Nasutkitut Apartments", "Novocherkasskiy bulvar; 46"),
        hotel("8684593", "Nasutkitut Apartments", "ulitsa Novonar'inskaya, 11/1"),
        # тот же дом, но дальше 30 м
        hotel("1", "Sea View", "Kurortny prospekt 1"),
        hotel("2", "Sea View", "Kurortny prospekt 1", lat=43.501),
    ]
    rooms = [{"id": "8747284:a", "hotel_id": "8747284", "rg_hash": "a"}]
    (item,) = duplicate_listings(hotels, rooms)
    assert item["kind"] == "duplicate_listing" and item["state"] == "proposed"
    assert item["proposal"]["hotel_ids"] == ["8661518", "8747284"]
    assert item["proposal"]["primary_hotel_id"] == "8747284"  # карточка с категориями
    assert item["proposal"]["merge_automatically"] is False
    assert item == duplicate_listings(list(reversed(hotels)), rooms)[0]
    validate_parts({"hotels": hotels, "facts": [], "curation_items": [item]})
