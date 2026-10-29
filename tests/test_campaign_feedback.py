"""Verified development feedback closes the loop without teaching from held-out answers."""

import json
from pathlib import Path

from radar.domain.models import Correction
from radar.evaluation.campaign import load_campaign, prepare
from radar.pipeline.corrections import reviewed_references
from radar.quality.campaign_feedback import apply_consensus
from radar.quality.errors import analyse
from radar.store.blobs import BlobStore
from radar.store.db import Database, utcnow
from radar.store.review_history import ReviewHistory
from tests.test_annotation_campaign import packet, seed


def test_verified_consensus_becomes_immutable_reference_and_repeat_is_noop(
    db: Database,
    blobs: BlobStore,
    tmp_path: Path,
) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    annotations = packet(directory)
    campaign = load_campaign(directory)
    result = apply_consensus(db, campaign, [annotations], blobs, reviewed_seconds=42)
    assert result["applied"] == 1 and result["rebuild_required"]
    _, fields = reviewed_references(db.filings(), db.corrections())
    assert fields[filing.key].expected["power_mw"] == 0.05
    assert db.filing(filing.key).machine.values["power_mw"] == 0.05
    # Dirty processing after applying a correction must not make the same receipt non-idempotent.
    assert apply_consensus(db, campaign, [annotations], blobs)["status"] == "already_applied"
    assert len(db.corrections()) == 1
    assert ReviewHistory(db.conn).summary()["declared_review_seconds"] == 42


def test_withheld_references_are_never_imported_into_learning(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs, partition="test")
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    result = apply_consensus(db, load_campaign(directory), [packet(directory)], blobs)
    assert result["applied"] == 0 and result["withheld_fields"] == 1
    assert db.corrections() == []


def test_conflicting_prior_human_value_prevents_entire_application(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    with db.transaction():
        db.add_correction(
            Correction(filing_key=filing.key, field="power_mw", value=99, author="Prior reviewer", created_at=utcnow())
        )
        db.note_processing_outcome(filing.key, "complete")
    result = apply_consensus(db, load_campaign(directory), [packet(directory)], blobs)
    assert "error" in result and result["applied"] == 0
    assert len(db.corrections()) == 1 and ReviewHistory(db.conn).summary()["receipts"] == 0


def test_double_review_pending_or_drafts_never_create_corrections(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    seed(db, blobs)
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=1)
    campaign = load_campaign(directory)
    path = packet(directory)
    assert apply_consensus(db, campaign, [path], blobs)["applied"] == 0
    row = json.loads(path.read_text())
    row["verified"] = False
    path.write_text(json.dumps(row))
    assert apply_consensus(db, campaign, [path], blobs)["applied"] == 0
    assert db.corrections() == []


def test_applied_human_observation_exposes_machine_error_without_overwriting_prediction(
    db: Database,
    blobs: BlobStore,
    tmp_path: Path,
) -> None:
    original = seed(db, blobs)
    # Deliberately incorrect synthetic machine output; the source still states 0.05 MW.
    broken = original.model_copy(update={"machine": original.machine.model_copy(update={"values": {"power_mw": 1.5}})})
    with db.transaction():
        db.upsert_filing(broken)
        db.note_processing_outcome(broken.key, "complete")
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    result = apply_consensus(db, load_campaign(directory), [packet(directory)], blobs)
    assert result["applied"] == 1
    errors = analyse(db.filings(), db.corrections())
    assert errors.with_errors == 1
    assert errors.errors[0].machine == 1.5 and errors.errors[0].human == 0.05
    assert db.filing(broken.key).machine.values["power_mw"] == 1.5
