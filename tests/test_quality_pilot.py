"""Synthetic protocol fixtures exercise pilots; these are not real human business references."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.config import Settings
from radar.domain.enums import SourceKind
from radar.domain.models import LinkDecision, Power
from radar.evaluation.campaign import check_documents, digest, load_campaign
from radar.evaluation.campaign_compare import load_snapshot
from radar.pilot_cli import cmd_pilot, register
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import link_and_build
from radar.quality import pilot
from radar.reading.pdf import PdfContent, read_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.test_annotation_campaign import seed


def spec_file(tmp_path: Path, *, verified: bool = False) -> Path:
    inventory = tmp_path / "inventory.jsonl"
    rows = [
        {
            "project_group": "external-a",
            "source": "dreal",
            "region": "hauts-de-france",
            "dossier_url": "https://example.org/dossier-a",
            "document_urls": ["https://example.org/form.pdf"],
            "relevant": True,
            "reviewer": "synthetic-inventory-reader" if verified else "",
            "reviewed_on": "2026-10-30" if verified else "",
            "verified": verified,
        },
        {
            "project_group": "external-b",
            "source": "mrae",
            "region": "normandie",
            "dossier_url": "https://example.org/missing",
            "document_urls": ["https://example.org/missing.pdf"],
            "relevant": True,
            "reviewer": "synthetic-inventory-reader" if verified else "",
            "reviewed_on": "2026-10-30" if verified else "",
            "verified": verified,
        },
    ]
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows))
    value = {
        "inventory": "inventory.jsonl",
        "provenance": {
            "basis": "external_manual_inventory",
            "assembled_by": "synthetic-protocol-fixture",
            "assembled_on": "2026-10-30",
            "references": ["https://example.org/independent-index"],
            "inclusion_protocol": "Synthetic inclusion protocol for offline tests only",
            "independence_rationale": "A declared protocol is not proof of actual independence",
        },
        "regions": ["hauts-de-france"],
        "formats": ["pdf", "docx"],
        "max_groups": 2,
        "max_documents": 10,
        "max_bytes": 1000000,
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(value))
    return path


def observe(db: Database, key: str) -> None:
    with db.transaction():
        db.save_dossier_scan(key, "https://example.org/dossier-a", {"state": "complete"})


def test_plan_keeps_full_groups_outside_filters_and_all_budgets(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    first = seed(db, blobs)
    second = seed(db, blobs, number="000002")
    # A distinct source/region is related by an explicit human grouping decision.
    second_key = "mrae_opinion:normandie:000002"
    with db.transaction():
        original = db.documents(second.key)[0]
        document = ingest_pdf(
            db,
            blobs,
            data=blobs.get(original.id),
            source=SourceKind.MRAE_OPINION,
            region="normandie",
            number="000002",
            url=original.url,
            title=original.title,
            filename=original.filename,
        )
        db.upsert_filing(
            second.model_copy(update={"key": second_key, "source": document.source, "region": "normandie"})
        )
        db.note_processing_outcome(second_key, "complete")
        db.add_link_decision(
            LinkDecision(
                a=first.key, b=second_key, verdict="same_project", author="fixture", created_at=datetime.now(UTC)
            )
        )
    spec = pilot.load_spec(spec_file(tmp_path))
    report = pilot.plan(db, spec, blobs=blobs)
    assert first.key in report["selected_keys"] and second_key in report["included_outside_filter"]
    assert report["strata"]["format"]["unrepresented"] == ["docx"]
    assert not report["quality_measured"] and report["network_requests"] == 0
    assert not list(tmp_path.glob(".pilot-*"))
    bounded = pilot.plan(db, spec.model_copy(update={"max_documents": 1}), blobs=blobs)
    assert bounded["selected_keys"] == [] and bounded["excluded_groups"] == {"document_budget": 1}
    bounded = pilot.plan(db, spec.model_copy(update={"max_bytes": 1}), blobs=blobs)
    assert bounded["selected_keys"] == [] and bounded["excluded_groups"] == {"byte_budget": 1}


def test_prepare_freezes_machine_inventory_and_blank_double_reviews(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    observe(db, filing.key)
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"power": Power(mw_max=99.0)}))
    spec = pilot.load_spec(spec_file(tmp_path)).model_copy(update={"prospective_after": "2000-01-01"})
    directory = tmp_path / "pilot"
    result = pilot.prepare(db, directory, spec, blobs)
    frozen = load_campaign(directory)
    assert frozen.records[0].partition == "test"
    assert all(record.double_review for record in frozen.records)
    initial = load_snapshot(directory / "initial.json", frozen)
    assert initial.predictions[0].values["power_mw"] == 0.05
    assert result["quality_measured"] is False
    for slot in ("a", "b"):
        row = json.loads((directory / f"reviewer-{slot}.jsonl").read_text())
        assert row["reviewer"] == "" and not row["verified"]
        assert all(value["status"] == "unreviewed" for value in row["fields"].values())
    assignments = [json.loads(line) for line in (directory / "assignments.jsonl").read_text().splitlines()]
    assert len(assignments) == 2 and all(row["reviewer"] == "" for row in assignments)
    assert (directory / "effort.jsonl").read_bytes() == b""
    assert len(db.corrections()) == 0
    with pytest.raises(ValueError, match="already exists"):
        pilot.prepare(db, directory, spec, blobs)
    status = pilot.status(db, directory, blobs, read=lambda _, data: read_pdf(data))
    assert status["status"] == "awaiting_human_review"
    assert status["stages"]["expected"] == 2 and status["stages"]["downloaded"] == 1
    assert status["stages"]["readable"] == 1 and status["stages"]["processed"] == 1
    assert status["pieces"] == {"expected": 2, "discovered": 1, "downloaded": 1, "readable": 1, "processed": 1}
    assert status["stages"]["assigned"] == status["stages"]["annotated"] == 0
    assert status["blocked_reasons"] == {"never_observed": 1}
    assert status["human_effort"]["seconds"] is None
    assert not status["quality_measured"] and status["values"]["partitions"] == {}


def test_status_uses_external_inventory_including_missing_cases_and_reports_explicit_effort(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    observe(db, filing.key)
    spec = pilot.load_spec(spec_file(tmp_path, verified=True))
    directory = tmp_path / "pilot"
    pilot.prepare(db, directory, spec, blobs)
    frozen = load_campaign(directory)
    (directory / "effort.jsonl").write_text(
        json.dumps(
            {
                "campaign_fingerprint": frozen.fingerprint,
                "session_id": "synthetic-session-1",
                "reviewer": "synthetic-reviewer-a",
                "phase": "review",
                "seconds": 42.0,
                "filing_key": filing.key,
            }
        )
        + "\n"
    )
    status = pilot.status(db, directory, blobs, read=lambda _, data: read_pdf(data))
    assert status["discovery"]["by_source"]["dreal"]["case_recall"] == 1.0
    assert status["discovery"]["by_source"]["mrae"]["case_recall"] == 0.0
    assert status["human_effort"]["seconds"] == 42.0
    assert status["grouping"]["status"] == "unmeasured"
    assert status["values"]["status"] == "unmeasured"
    assert status["annotation"]["verified_review_rows"] == 0
    original = (directory / "inventory.jsonl").read_bytes()
    (directory / "inventory.jsonl").write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="inventory changed"):
        pilot.status(db, directory, blobs)


def test_prepare_failure_is_atomic_and_corrupt_originals_are_rejected(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    spec = pilot.load_spec(spec_file(tmp_path))
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"machine": None}))
    directory = tmp_path / "pilot"
    with pytest.raises(ValueError, match="raw machine"):
        pilot.prepare(db, directory, spec, blobs)
    assert not directory.exists() and not list(tmp_path.glob(".pilot-*"))
    blobs.path_for(filing.document_ids[0]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        pilot.prepare(db, directory, spec, blobs)
    assert not directory.exists()


def test_manifest_changes_and_duplicate_reviewers_are_visible(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    filing = seed(db, blobs)
    spec = pilot.load_spec(spec_file(tmp_path))
    directory = tmp_path / "pilot"
    pilot.prepare(db, directory, spec, blobs)
    with db.transaction():
        db.note_processing_outcome(filing.key, "failed", "fixture failure")
    report = pilot.status(db, directory, blobs)
    assert report["status"] == "stale" and not report["quality_measured"]
    rows = [json.loads(line) for line in (directory / "assignments.jsonl").read_text().splitlines()]
    for row in rows:
        row["reviewer"] = "same fixture reader"
    (directory / "assignments.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="distinct readers"):
        pilot.status(db, directory, blobs)


def test_grouping_uses_independent_groups_and_keeps_all_pairs(db: Database, blobs: BlobStore) -> None:
    filings = [seed(db, blobs, number=f"00000{number}") for number in range(1, 4)]
    link_and_build(db)
    projects = {project.filing_keys[0]: project for project in db.projects()}
    first, second, third = filings
    with db.transaction():
        db.replace_projects(
            [
                projects[first.key].model_copy(update={"filing_keys": (first.key, second.key)}),
                projects[third.key],
            ]
        )
        for index, filing in enumerate(filings):
            db.save_dossier_scan(filing.key, f"https://example.org/root-{index}", {"state": "complete"})
    inventory = [
        pilot.discovery.DiscoveryReference(
            project_group="same" if index != 1 else "different",
            source="dreal",
            region="hauts-de-france",
            dossier_url=f"https://example.org/root-{index}",
            relevant=True,
            verified=True,
            reviewer="synthetic-reader",
            reviewed_on="2026-10-30",
        )
        for index in range(3)
    ]
    report = pilot._grouping(db, inventory)
    assert report["expected_same_pairs"] == 1 and report["expected_distinct_pairs"] == 2
    assert report["false_merges"] == 1 and report["false_splits"] == 1


def test_legacy_pdf_campaigns_load_and_new_archive_provenance_is_frozen(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    spec = pilot.load_spec(spec_file(tmp_path))
    directory = tmp_path / "pilot"
    pilot.prepare(db, directory, spec, blobs)
    payload = json.loads((directory / "manifest.json").read_text())
    for record in payload["records"]:
        for document in record["documents"]:
            for key in ("format", "parent_document_id", "member_path"):
                document.pop(key)
    assert payload["fingerprint"] == digest({key: value for key, value in payload.items() if key != "fingerprint"})
    (directory / "manifest.json").write_text(json.dumps(payload))
    frozen = load_campaign(directory)
    document = db.documents(filing.key)[0]
    with db.transaction():
        db.upsert_document(
            document.model_copy(update={"parent_document_id": "archive-sha", "member_path": "annex.pdf"})
        )
    with pytest.raises(ValueError, match="metadata changed"):
        check_documents(db, frozen)


def test_partial_formats_and_reading_budgets_are_explicitly_incomplete(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    observe(db, filing.key)
    directory = tmp_path / "pilot"
    pilot.prepare(db, directory, pilot.load_spec(spec_file(tmp_path)), blobs)
    report = pilot.status(
        db,
        directory,
        blobs,
        read=lambda _, data: PdfContent(
            pages=1,
            fields={},
            page_texts=("Readable partial document",),
            scanned_pages=(),
            format_errors=("Formula result is unverified",),
            useful_unread_pages=(1,),
            table_errors={1: "Budget exceeded"},
        ),
    )
    assert report["stages"]["readable"] == 0
    assert report["blocked_reasons"]["format:Formula result is unverified"] == 1
    assert report["blocked_reasons"]["useful_pages_unread"] == 1
    assert report["blocked_reasons"]["table_reading_incomplete"] == 1


def test_initial_snapshot_is_bound_to_pilot_manifest(db: Database, blobs: BlobStore, tmp_path: Path) -> None:
    seed(db, blobs)
    directory = tmp_path / "pilot"
    pilot.prepare(db, directory, pilot.load_spec(spec_file(tmp_path)), blobs)
    frozen = load_campaign(directory)
    snapshot = load_snapshot(directory / "initial.json", frozen)
    changed = snapshot.model_copy(update={"name": "silently-replaced"})
    changed = changed.model_copy(update={"fingerprint": changed.digest()})
    (directory / "initial.json").write_text(changed.model_dump_json())
    with pytest.raises(ValueError, match="initial machine snapshot changed"):
        pilot.status(db, directory, blobs)


def test_origin_is_required_and_cli_plan_is_headless(
    db: Database, blobs: BlobStore, settings: Settings, tmp_path: Path
) -> None:
    seed(db, blobs)
    path = spec_file(tmp_path)
    value = json.loads(path.read_text())
    value["provenance"]["independence_rationale"] = " "
    with pytest.raises(ValueError, match="nonblank"):
        pilot.PilotSpec.model_validate_json(json.dumps(value))
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["pilot", "plan", "--spec", str(path)])
    report = cmd_pilot(args, settings)
    assert report["selected_groups"] == 1
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["pilot", "prepare"])
    assert result.value.code == 2
