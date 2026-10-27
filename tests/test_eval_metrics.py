"""`tests/eval/metrics.py` — метрики для слайдов (ревью №22).

Проверяем то, что уже ломалось: запуск без фактов (`KeyError: 'by_city'`), покрытие, где оценки
считались «известными», пакет с двумя файлами фактов (слой «вид и солнце») и метрика разбора,
которая сверяла строки целиком и выдавала «100%» на условиях с одними кодами.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parent / "eval"


@pytest.fixture(scope="module")
def metrics():
    spec = importlib.util.spec_from_file_location("eval_metrics", EVAL / "metrics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fact(hotel: str, feature: str, status: str) -> dict:
    return {"hotel_id": hotel, "city_id": "sochi", "feature_id": feature, "status": status}


def write_gz(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def test_without_facts_reports_annotations_and_distances(metrics, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "metrics.py",
            "--annotations",
            str(EVAL / "annotations.csv"),
            "--distances",
            str(EVAL / "distances.csv"),
        ],
    )
    assert metrics.main() == 0
    out = capsys.readouterr().out
    assert "покрытие и вопросы не считались" in out
    assert "медиана модулей 110.2 м" in out and "медиана +96.2 м" in out
    assert "проверил ИИ-ассистент" in out  # разметка ИИ не выдаётся за проверку человеком


def test_estimates_are_not_hard_coverage(metrics):
    rows = [
        fact("1", "B02", "estimate"),
        fact("2", "B02", "estimate"),
        fact("1", "C01", "computed"),
        fact("2", "C01", "unknown"),
    ]
    cov = metrics.coverage(rows, "sochi")
    assert cov["B02"]["coverage"] == 1.0 and cov["B02"]["coverage_without_estimates"] == 0.0
    assert cov["C01"]["coverage"] == cov["C01"]["coverage_without_estimates"] == 0.5


def test_pack_facts_are_read_from_every_manifest_file(metrics, tmp_path):
    pack = tmp_path / "packs" / "sochi"
    write_gz(pack / "data" / "facts.jsonl.gz", [fact("1", "C01", "computed")])
    write_gz(pack / "data" / "view_sun.jsonl.gz", [fact("1", "V05", "computed")])
    write_gz(pack / "data" / "questions.jsonl.gz", [{"hotel_id": "1", "feature_id": "A02"}])
    manifest = {
        "files": [
            {"path": "data/facts.jsonl.gz", "kind": "facts"},
            {"path": "data/view_sun.jsonl.gz", "kind": "facts", "layer": True},
            {"path": "data/questions.jsonl.gz", "kind": "questions"},
        ]
    }
    (pack / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    files = metrics.pack_files("sochi", "facts", root=tmp_path)
    assert [p.name for p in files] == ["facts.jsonl.gz", "view_sun.jsonl.gz"]
    facts = [f for p in files for f in metrics.iter_facts(p)]
    assert {f["feature_id"] for f in facts} == {"C01", "V05"}
    assert [p.name for p in metrics.pack_files("sochi", "questions", root=tmp_path)] == [
        "questions.jsonl.gz"
    ]
    assert metrics.pack_files("nowhere", "facts", root=tmp_path) == []


def test_parse_is_precision_and_recall_not_row_match(metrics, tmp_path):
    """Формат `agents.eval.parse_eval --csv`: найдено, пропущено, лишнее, неверный код."""
    path = tmp_path / "parse.csv"
    rows = [
        {"expected": "W01", "got": "W01", "requiredness": "hard"},  # найдено
        {"expected": "S05", "got": "", "requiredness": "hard"},  # пропущено
        {"expected": "", "got": "N04", "requiredness": "hard"},  # лишнее
        {"expected": "N02", "got": "N05", "requiredness": "hard"},  # не тот код: оба промаха
        {"expected": "C01", "got": "C01", "requiredness": "soft"},
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["expected", "got", "requiredness"])
        writer.writeheader()
        writer.writerows(rows)
    parse = metrics.parse_accuracy(path)
    hard = parse["hard"]
    assert (hard["found"], hard["missed"], hard["extra"], hard["expected"]) == (1, 2, 2, 3)
    assert hard["precision"] == pytest.approx(1 / 3) and hard["recall"] == pytest.approx(1 / 3)
    assert parse["soft"]["precision"] == parse["soft"]["recall"] == 1.0
    report = metrics.markdown({}, {}, None, None, parse, None)
    assert "только код характеристики" in report and "точность 33%, полнота 33%" in report
