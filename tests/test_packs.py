import gzip
import json
from datetime import UTC, datetime

import pytest

from app.stub_store import StubStore
from core.packs import Manifest, PackError, PackFile, sha256_file, verify
from pipeline.load_packs import fact_row


def make_pack(tmp_path):
    store = StubStore.load()
    facts = list(store.facts.values())[:5]
    data = tmp_path / "data"
    data.mkdir()
    path = data / "facts.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for fact in facts:
            f.write(fact.model_dump_json() + "\n")
    manifest = Manifest(
        city_id="sochi",
        city_name="Большой Сочи",
        timezone="Europe/Moscow",
        depth="full",
        pack_version="test.1",
        built_at=datetime.now(UTC),
        files=[
            PackFile(
                path="data/facts.jsonl.gz",
                kind="facts",
                rows=len(facts),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        ],
    )
    (tmp_path / "manifest.json").write_text(manifest.model_dump_json(indent=2), "utf-8")
    return facts


def test_verify_ok_and_detects_tampering(tmp_path):
    make_pack(tmp_path)
    assert verify(tmp_path).city_id == "sochi"
    with gzip.open(tmp_path / "data" / "facts.jsonl.gz", "at", encoding="utf-8") as f:
        f.write("{}\n")
    with pytest.raises(PackError):
        verify(tmp_path)


def test_fact_row_splits_confidence_and_keeps_extra():
    raw = json.loads(next(iter(StubStore.load().facts.values())).model_dump_json())
    raw["component_diagnostics"] = {"x": 1}
    row = fact_row(raw)
    assert "confidence" not in row
    assert row["confidence_level"] in (None, "high", "medium", "low", "unknown")
    assert row["extra"]["component_diagnostics"] == {"x": 1}
