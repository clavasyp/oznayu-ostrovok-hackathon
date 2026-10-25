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
