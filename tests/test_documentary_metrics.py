"""Operational counts must not turn missing reviews, machine predictions or unknown costs into evidence."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from radar.domain.models import Correction
from radar.quality.documentary_metrics import build_quality_loop
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.test_annotation_campaign import seed


def correction(db: Database, key: str, name: str, value: str | float | None) -> None:
    with db.transaction():
        db.add_correction(
            Correction(
                filing_key=key,
                field=name,
                value=value,
                author="PRIVATE REVIEWER",
                created_at=datetime.now(UTC),
                blind=True,
            )
        )


def journal(path: Path, *, unpriced: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE attempts(actual_micros INTEGER,reserved_micros INTEGER);CREATE TABLE jobs(state TEXT);"
        )
        connection.execute("INSERT INTO attempts VALUES (1000000,2000000)")
        if unpriced:
            connection.execute("INSERT INTO attempts VALUES (NULL,3000000)")
        connection.execute("INSERT INTO jobs VALUES ('succeeded')")


def test_empty_diagnostics_do_not_invent_quality_cost_or_reading(db: Database, tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite"
    report = build_quality_loop(db, jobs_path=absent)
    assert not report["quality_measured"] and report["sources"] == {}
    assert report["reading"]["counters"]["pages"] is None
    assert report["model_cost"]["priced_usage_usd"] is None and not absent.exists()
    assert report["human_feedback"]["declared_review_seconds"] is None


def test_random_audit_keeps_unmapped_and_unreviewed_in_denominator(db: Database, blobs: BlobStore) -> None:
    filing = seed(db, blobs)
    source = filing.source.value
    with db.transaction():
        db.save_dossier_scan(filing.key, "https://example.org/random", {"state": "partial"})
        db.record_listing_item("https://example.org/random", source, "x", False, "random")
        db.record_listing_item("https://example.org/not-downloaded", source, "x", False, "random")
        db.record_listing_item("https://example.org/near", source, "x", False, "near-miss:solar-wind")
    before = build_quality_loop(db)["sources"][source]
    assert before["listings"]["random_selected"] == 2 and before["listings"]["near_miss_selected"] == 1
    assert before["random_audit"]["mapped_downloaded"] == 1
    assert before["random_audit"]["reviewed"] == 0  # machine BESS is not human gold
    correction(db, filing.key, "category", "bess")
    audit = build_quality_loop(db)["sources"][source]["random_audit"]
    assert audit["reviewed"] == 1 and audit["targets"] == 1 and audit["unmapped"] == 1
    assert audit["not_human_reviewed_global"] == 1 and audit["human_response_share_global"] == 0.5
    assert audit["population_recall"] is None


def test_shared_pdf_counts_one_group_and_keeps_heldout_details_private(db: Database, blobs: BlobStore) -> None:
    first = seed(db, blobs)
    second = first.model_copy(
        update={
            "key": "dreal_case_by_case:hauts-de-france:000002",
            "number": "000002",
            "strata": first.strata | {"evaluation_partition": "test"},
            "machine": first.machine.model_copy(update={"values": {"power_mw": 0.1}}) if first.machine else None,
        }
    )
    document = db.documents(first.key)[0]
    with db.transaction():
        db.upsert_document(document.model_copy(update={"filing_key": second.key}))
        db.upsert_filing(second)
    for filing in (first, second):
        correction(db, filing.key, "power_mw", 0.05)
    report = build_quality_loop(db)
    score = report["sources"][first.source.value]["verified_fields"]["test"]["power_mw"]
    assert score["observations"] == 2 and score["independent_groups"] == 1 and score["exact_group_accuracy"] == 0
    serialized = json.dumps(report)
    assert "PRIVATE REVIEWER" not in serialized and first.key not in serialized and second.key not in serialized
    assert "000002" not in serialized and "quote" not in serialized


def test_field_score_requires_raw_machine_output(db: Database, blobs: BlobStore) -> None:
    filing = seed(db, blobs)
    correction(db, filing.key, "power_mw", 0.05)
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"machine": None}))
    report = build_quality_loop(db)
    assert not report["quality_measured"]
    assert "verified_fields" not in report["sources"].get(filing.source.value, {})


def test_rejected_population_without_selected_audits_has_zero_counts_not_null(db: Database) -> None:
    with db.transaction():
        db.record_listing_item("https://example.org/rejected", "mrae_opinion", "x", False, None)
    report = build_quality_loop(db)["sources"]["mrae_opinion"]
    assert report["listings"]["random_selected"] == 0
    assert report["random_audit"]["human_response_share_global"] is None


def test_quality_overview_exposes_read_only_diagnostics(db: Database, tmp_path: Path) -> None:
    from radar.api.quality import quality_overview

    before = db.conn.total_changes
    report = quality_overview(db, tmp_path / "cases")
    assert "documentary" in report and not report["documentary"]["quality_measured"]
    assert db.conn.total_changes == before


def test_unpriced_attempt_stays_unknown_and_receipt_scope_is_explicit(db: Database, tmp_path: Path) -> None:
    path = tmp_path / "jobs.sqlite"
    journal(path, unpriced=True)
    report = build_quality_loop(db, jobs_path=path)
    costs = report["model_cost"]
    assert costs["status"] == "partial" and costs["unpriced_attempts"] == 1
    assert costs["known_priced_usage_usd"] == 1 and costs["priced_usage_usd"] is None
    assert costs["priced_usage_per_complete_target_group_usd"] is None


def test_unit_cost_requires_complete_human_target_groups_and_matching_scope(
    db: Database, blobs: BlobStore, tmp_path: Path
) -> None:
    filing = seed(db, blobs)
    for name, value in {
        "category": "bess",
        "developer": "SYNTHETIC SAS",
        "commune": "Test",
        "power_mw": 0.05,
        "filed_on": "2026-10-29",
        "review_decision": "not_subject",
    }.items():
        correction(db, filing.key, name, value)
    path = tmp_path / "jobs.sqlite"
    journal(path)
    report = build_quality_loop(db, jobs_path=path)
    assert report["human_complete_target_groups"] == 1
    assert report["model_cost"]["priced_usage_per_complete_target_group_usd"] == 1
    regional = build_quality_loop(db, jobs_path=path, region="hauts-de-france")
    assert regional["model_cost"]["priced_usage_per_complete_target_group_usd"] is None


def test_freshness_and_external_attachment_counts_keep_their_actual_scope(db: Database, blobs: BlobStore) -> None:
    filing = seed(db, blobs)
    with db.transaction():
        db.save_collection_coverage(
            {
                "source": "dreal",
                "region": "hauts-de-france",
                "period": "2025",
                "scope": "listing",
                "mode": "historical",
                "state": "complete",
                "completed_at": "2020-01-01T00:00:00+00:00",
                "updated_at": "2099-01-01T00:00:00+00:00",
            }
        )
        db.save_dossier_scan(filing.key, "https://example.org/root", {"state": "partial"})
        for parent in ("https://example.org/a", "https://example.org/b"):
            db.record_dossier_edge(
                filing.key, "https://example.org/root", parent, "https://external.example/annexe.pdf", "outside_scope"
            )
        db.upsert_filing(
            filing.model_copy(
                update={
                    "strata": filing.strata
                    | {
                        "reading_pages": "3",
                        "reading_table_rows": "invalid",
                        "reading_unread_pages": "1",
                    },
                    "unread_scans": filing.document_ids,
                }
            )
        )
    report = build_quality_loop(db)
    source = report["sources"][filing.source.value]
    assert source["coverage"]["stale_complete_scopes"] == 1
    assert source["dossiers"] == {"partial": 1, "outside_scope_edges": 2, "outside_scope_attachments": 1}
    assert report["reading"]["counters"]["pages"] == 3
    assert report["reading"]["counters"]["table_rows"] is None
    assert report["reading"]["unread_document_ids"] == 1
