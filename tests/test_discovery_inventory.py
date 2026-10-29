"""Known projects missing before ingestion must remain in the discovery denominator."""

import json
from pathlib import Path

import pytest

from radar.domain.enums import SourceKind
from radar.evaluation.discovery import evaluate
from radar.pipeline.ingest import ingest_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import FILLER, make_pdf


def test_discovery_includes_never_observed_and_rejected_cases(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    with db.transaction():
        document = ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER]),
            source=SourceKind.MRAE_OPINION,
            region="r",
            number="1",
            url="https://example.org/opinion.pdf",
            title="Avis",
            filename="a.pdf",
        )
        db.save_dossier_scan(document.filing_key, "https://example.org/case", {"state": "partial"})
        db.record_listing_item("https://example.org/rejected", SourceKind.MRAE_OPINION.value, "x", False, None)
    references = [
        {
            "project_group": "shared",
            "source": "mrae",
            "region": "r",
            "dossier_url": "https://example.org/" + root,
            "document_urls": urls,
            "relevant": True,
            "reviewer": "human",
            "reviewed_on": "2026-10-29",
            "verified": True,
        }
        for root, urls in (
            ("case", [document.url, "https://example.org/missing.pdf"]),
            ("rejected", []),
            ("unseen", []),
        )
    ]
    path = tmp_path / "inventory.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in references))
    score = evaluate(db, path)["by_source"]["mrae"]
    assert score["case_recall"] == 1 / 3 and score["expected_document_recall"] == 0.5
    assert score["independent_groups"] == 1 and score["independent_group_recall"] == 0
    assert score["missing_case_stages"] == {"listed_not_downloaded": 1, "never_observed": 1}


def test_empty_or_draft_inventory_has_no_positive_score(db: Database, tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    report = evaluate(db, path)
    assert not report["quality_measured"] and report["by_source"] == {}
    path.write_text(
        json.dumps(
            {
                "project_group": "x",
                "source": "dreal",
                "region": "r",
                "dossier_url": "https://example.org/case",
                "relevant": True,
            }
        )
    )
    assert not evaluate(db, path)["quality_measured"]


def test_inventory_rejects_duplicates_and_unattributed_verification(db: Database, tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    row = {
        "project_group": "x",
        "source": "dreal",
        "region": "r",
        "dossier_url": "https://example.org/case",
        "relevant": True,
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps(row))
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate(db, path)
    row["verified"] = True
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="reviewer"):
        evaluate(db, path)
