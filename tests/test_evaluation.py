"""Evaluation bench: field accuracy, OCR word error rate, classifier confusion, run-to-run regressions."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from radar.classification.base import ClassificationInput
from radar.classification.gold import GoldLabel
from radar.cli import main
from radar.domain.enums import Category
from radar.domain.models import Classification
from radar.evaluation.classification import evaluate_classifier
from radar.evaluation.fields import compare, score_fields
from radar.evaluation.gold import FieldGold, load_field_gold, write_field_gold
from radar.evaluation.ocr import evaluate_ocr, word_error_rate, word_miss_rate
from radar.evaluation.report import compare as compare_metrics
from radar.pipeline.filings import Services
from radar.reading.ocr import NullOcr
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import make_pdf

AUBENTON = "010504"


def test_compare_outcomes() -> None:
    assert compare(47, 47.2) == "correct" and compare(47, 52) == "wrong"
    assert compare("Les Hoquins", "les hoquins") == "correct"
    assert (
        compare("Margot", None) == "missed" and compare(None, "400kV") == "spurious" and compare(None, None) == "absent"
    )


def test_field_scores_separate_verified_and_drafts(loaded_db: Database, tmp_path: Path) -> None:
    gold = {
        AUBENTON: FieldGold(
            AUBENTON, {"power_mw": 100, "substation": "Les Hoquins", "siret": None}, "Dan", verified=True
        ),
        "2025-4009": FieldGold("2025-4009", {"developer": "Autre société"}, "draft:extraction-x"),
    }
    path = tmp_path / "gold_fields.jsonl"
    write_field_gold(path, gold.values())
    assert load_field_gold(path) == gold
    verified = score_fields(loaded_db.filings(), gold, verified=True)
    assert verified["power_mw"].accuracy == 1.0 and verified["substation"].accuracy == 1.0
    assert verified["siret"].spurious == 1  # the reference says "absent", we extracted one
    draft = score_fields(loaded_db.filings(), gold, verified=False)
    assert draft["developer"].wrong == 1 and "2025-4009" in draft["developer"].errors[0]


def test_ocr_metrics() -> None:
    assert word_error_rate("Le projet n'est pas soumis", "le projet n’est pas soumis") == 0.0
    assert word_error_rate("un deux trois quatre", "un deux trios quatre") == 0.25
    page = "Préfet de région Vu le formulaire d'examen Article 1 le projet"
    assert word_miss_rate("Article 1 vu le formulaire", page) == 0.0  # excerpts, any order, extra words free
    assert word_miss_rate("non soumission", "nan soumission") == 0.5


class FakeOcr:
    label = "fake"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        return replace(content, ocr_texts=dict.fromkeys(content.scanned_pages, "vu le formulaire numero"))


def test_ocr_evaluation(blobs: BlobStore, tmp_path: Path) -> None:
    digest = blobs.put(make_pdf([""]), ".pdf")
    (tmp_path / f"{digest}.txt").write_text("Vu le formulaire n°", encoding="utf-8")
    result = evaluate_ocr(FakeOcr(), blobs, tmp_path)
    assert (
        result["documents"] == 1 and result["mean_word_miss_rate"] == 0.25
    )  # « n° » folded to « n », read as « numero »


class FixedClassifier:
    name = "fake-llm"

    def classify(self, item: ClassificationInput) -> Classification | None:
        return Classification(category=Category.BESS, method=self.name, quote="stockage", quote_verified=True)


def test_classifier_evaluation(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    gold = {AUBENTON: GoldLabel(AUBENTON, Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)}
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=lookups)
    report = evaluate_classifier(FixedClassifier(), loaded_db, services, gold)
    assert report.evaluated == 2 and report.correct == 1 and report.accuracy == 0.5
    assert report.confusion == {"bess": {"bess": 1}, "datacenter": {"bess": 1}}


def test_regression_detection() -> None:
    result = compare_metrics(
        {"screening.recall": 1.0, "privacy.leaks": 0.0, "ocr.mean_word_miss_rate": 0.1},
        {"screening.recall": 0.9, "privacy.leaks": 0.0, "ocr.mean_word_miss_rate": 0.05},
    )
    assert [r["metric"] for r in result["regressions"]] == ["screening.recall"]
    assert [r["metric"] for r in result["improvements"]] == ["ocr.mean_word_miss_rate"]


def test_eval_command_compares_with_previous_run(
    loaded_db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RADAR_DATA_DIR", str(loaded_db.path.parent))
    monkeypatch.setenv("RADAR_OFFLINE", "1")
    (loaded_db.path.parent / "gold_labels.jsonl").write_text(
        json.dumps({"number": AUBENTON, "category": "bess", "verified": True}) + "\n", encoding="utf-8"
    )
    assert main(["eval", "--draft-fields"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["gold"] == {"labels": 1, "labels_verified": 1, "fields": 1, "fields_verified": 0}
    assert first["fields_draft"]["power_mw"]["accuracy"] == 1.0 and first["privacy"]["leaks"] == []
    assert main(["eval", "--strict"]) == 0  # nothing changed: no regression
    second = json.loads(capsys.readouterr().out)
    assert second["comparison"] == {"regressions": [], "improvements": []}
