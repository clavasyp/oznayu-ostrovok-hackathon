"""Пояснения сверки с Островком — по-русски для каждого текста из пакетов (расчёт v9 — англ.)."""

import gzip
import json
from pathlib import Path

from app.api.routes import RESOLUTION_RU

ROOT = Path(__file__).resolve().parents[1]


def test_every_comparison_resolution_has_russian_text():
    seen = set()
    for path in (ROOT / "packs").glob("*/data/ota_comparisons.jsonl.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            seen |= {json.loads(line).get("resolution") for line in f}
    missing = [text for text in seen if text and text not in RESOLUTION_RU]
    assert seen and missing == []
