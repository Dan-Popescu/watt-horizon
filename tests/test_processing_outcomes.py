"""A successful partial rebuild cannot conceal unrelated unfinished document work."""

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from radar.domain.enums import DocumentRole, SignalKind, SourceKind
from radar.domain.models import Correction, DocumentRecord, LinkDecision, Signal
from radar.pipeline.filings import Services
from radar.pipeline.orchestration import execute_processing
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import NullOcr
from radar.reading.pdf import PdfContent, PdfReader
from radar.store.blobs import BlobStore
from radar.store.db import _MIGRATIONS, SCHEMA_VERSION, Database
from radar.store.readiness import candidate_readiness
from radar.validation.lookups import StaticLookups


class SyntheticReader(PdfReader):
    def __init__(self) -> None:
        super().__init__()
        self.repaired = False
        self.interrupt_prefetch = False
        self.reads: list[str] = []

    def prefetch(self, documents: list[tuple[str, Callable[[], bytes]]], workers: int | None = None) -> int:
        if self.interrupt_prefetch:
            raise KeyboardInterrupt("simulated interruption before document parsing")
        return 0

    def read(self, document_id: str, data: bytes) -> PdfContent:
        self.reads.append(document_id)
        if data == b"broken" and not self.repaired:
            raise ValueError("synthetic corrupt PDF")
        return PdfContent(
            pages=1, fields={}, page_texts=("Texte administratif synthétique de décision. " * 12,), scanned_pages=()
        )


def attach(db: Database, blobs: BlobStore, name: str, data: bytes) -> DocumentRecord:
    record = DocumentRecord(
        id=blobs.put(data), source=SourceKind.DREAL_CASE_BY_CASE, url=f"https://example.test/{name}.pdf",
        title="Décision synthétique", role=DocumentRole.DECISION,
        filing_key=f"dreal_case_by_case:r:{name}", fetched_at=datetime.now(UTC), filename=f"{name}.pdf",
    )  # fmt: skip
    with db.transaction():
        db.upsert_document(record)
    return record


def services_for(blobs: BlobStore, reader: SyntheticReader) -> Services:
    return Services(
        blobs=blobs, ocr=NullOcr(), classifier=None, lookups=StaticLookups(), reader=reader,
        fingerprint="synthetic-processing-outcome-v1",
    )  # fmt: skip


def process_and_link(db: Database, services: Services, *, only: set[str] | None = None) -> dict[str, Any]:
    result = execute_processing(db, services, only=only)
    link_and_build(db)
    return result


def test_successful_partial_run_does_not_erase_failed_outcome_and_real_retry_clears_it(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        broken = attach(db, blobs, "a", b"broken")
        healthy = attach(db, blobs, "b", b"healthy")
        services = services_for(blobs, reader)
        first = process_and_link(db, services)
        assert first["status"] == "blocked" and broken.filing_key in first["failed"]
        assert db.processing_outcome(broken.filing_key) == "failed"
        second = process_and_link(db, services, only={healthy.filing_key})
        assert second["status"] == "complete" and not second["failed"]
        assert list(db.unfinished_processing_keys()) == [broken.filing_key]
        assert not candidate_readiness(tmp_path)["ready"]
        reader.repaired = True
        recovered = process_and_link(db, services, only={broken.filing_key})
        assert recovered["status"] == "complete" and recovered["processed"] == 1
        assert list(db.unfinished_processing_keys()) == []
        assert candidate_readiness(tmp_path)["ready"]


def test_interruption_before_prefetch_leaves_a_committed_running_outcome(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        records = [attach(db, blobs, "a", b"healthy-a"), attach(db, blobs, "b", b"healthy-b")]
        reader.interrupt_prefetch = True
        with pytest.raises(KeyboardInterrupt, match="before document parsing"):
            execute_processing(db, services_for(blobs, reader))
        with Database(db.path, read_only=True) as observer:
            assert all(observer.processing_outcome(record.filing_key) == "running" for record in records)
        assert reader.reads == []
        assert not candidate_readiness(tmp_path)["ready"]


def test_incomplete_outcome_forces_rebuild_even_when_input_fingerprint_matches(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    reader.repaired = True
    with Database(tmp_path / "radar.sqlite") as db:
        record = attach(db, blobs, "a", b"broken")
        services = services_for(blobs, reader)
        assert process_filings(db, services).processed == 1
        stored = db.filing(record.filing_key)
        assert stored is not None and stored.versions.get("input")
        with db.transaction():
            db.note_processing_outcome(record.filing_key, "failed", "an earlier reconstruction failed")
        reader.repaired = False
        report = process_filings(db, services)
        assert record.filing_key in report.failed and report.unchanged == 0
        assert db.processing_outcome(record.filing_key) == "failed"
        reader.repaired = True
        assert process_filings(db, services).processed == 1
        assert db.processing_outcome(record.filing_key) == "complete"


def test_unchanged_complete_outcomes_are_not_rewritten(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        attach(db, blobs, "a", b"healthy")
        services = services_for(blobs, reader)
        assert process_filings(db, services).processed == 1
        writes = db.conn.total_changes
        report = process_filings(db, services)
        assert (report.processed, report.unchanged) == (0, 1)
        assert db.conn.total_changes == writes


def test_partial_run_does_not_hide_a_new_filing_or_new_document_attachment(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        a = attach(db, blobs, "a", b"healthy-a")
        b = attach(db, blobs, "b", b"healthy-b")
        services = services_for(blobs, reader)
        assert process_and_link(db, services)["status"] == "complete"
        new_case = attach(db, blobs, "c", b"healthy-c")
        assert process_and_link(db, services, only={b.filing_key})["status"] == "complete"
        assert not candidate_readiness(tmp_path)["ready"]
        assert process_and_link(db, services, only={new_case.filing_key})["status"] == "complete"
        assert candidate_readiness(tmp_path)["ready"]
        attach(db, blobs, "a", b"new-document-for-existing-case-a")
        assert process_and_link(db, services, only={b.filing_key})["status"] == "complete"
        assert not candidate_readiness(tmp_path)["ready"]
        assert process_and_link(db, services, only={a.filing_key})["status"] == "complete"
        assert candidate_readiness(tmp_path)["ready"]


def test_waiting_for_model_is_not_recorded_as_complete(tmp_path: Path) -> None:
    class WaitingQueue:
        pending = True

        def waiting(self, key: str) -> bool:
            return self.pending

    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    queue = WaitingQueue()
    with Database(tmp_path / "radar.sqlite") as db:
        record = attach(db, blobs, "a", b"healthy")
        services = replace(services_for(blobs, reader), llm_queue=queue)
        assert process_filings(db, services).processed == 1
        assert db.processing_outcome(record.filing_key) == "waiting"
        filing = db.filing(record.filing_key)
        assert filing is not None and "input" not in filing.versions
        queue.pending = False
        assert process_filings(db, services).processed == 1
        assert db.processing_outcome(record.filing_key) == "complete"


def test_outcome_pruning_requires_both_filing_and_attachments_to_be_absent(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        attached = attach(db, blobs, "a", b"broken")
        built = attach(db, blobs, "b", b"healthy")
        process_filings(db, services_for(blobs, reader))
        with db.transaction():
            db.conn.execute("DELETE FROM document_attachments WHERE filing_key=?", (built.filing_key,))
            db.note_processing_outcome("removed-case", "failed", "removed input")
            db.prune_processing_outcomes()
        assert db.processing_outcome(attached.filing_key) == "failed"  # only the attachment exists
        assert db.processing_outcome(built.filing_key) == "waiting"  # only the filing exists
        assert db.processing_outcome("removed-case") is None
        with db.transaction():
            db.conn.execute("DELETE FROM filings WHERE key=?", (built.filing_key,))
            db.prune_processing_outcomes()
        assert db.processing_outcome(built.filing_key) is None


def test_processing_outcomes_migrate_additively_and_obey_caller_transaction(tmp_path: Path) -> None:
    path = tmp_path / "radar.sqlite"
    with sqlite3.connect(path) as connection:
        for migration in _MIGRATIONS[:12]:
            connection.executescript(migration)
        connection.execute("PRAGMA user_version=12")
    with Database(path) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 13
        with pytest.raises(RuntimeError, match="abort"), db.transaction():
            db.note_processing_outcome("case", "running")
            raise RuntimeError("abort")
        assert db.processing_outcome("case") is None
        with db.transaction():
            db.note_processing_outcome("case", "failed", "synthetic failure")
    with Database(path, read_only=True) as reader:
        assert list(reader.unfinished_processing_keys()) == ["case"]


def test_attachment_metadata_invalidates_only_changed_inputs_and_deletion_is_durable(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        record = attach(db, blobs, "a", b"healthy")
        services = services_for(blobs, reader)
        process_filings(db, services)
        original_revision = db.processing_input_revision(record.filing_key)
        with db.transaction():
            db.upsert_document(record)
        assert db.processing_outcome(record.filing_key) == "complete"
        assert db.processing_input_revision(record.filing_key) == original_revision
        changed = record.model_copy(update={"title": "Un titre documentaire modifié"})
        with db.transaction():
            db.upsert_document(changed)
        assert db.processing_outcome(record.filing_key) == "waiting"
        assert db.processing_input_revision(record.filing_key) == original_revision + 1
        assert not candidate_readiness(tmp_path)["ready"]
        process_filings(db, services)
        assert db.processing_outcome(record.filing_key) == "complete"
        with db.transaction():
            db.conn.execute("DELETE FROM document_attachments WHERE filing_key=?", (record.filing_key,))
        assert db.processing_outcome(record.filing_key) == "waiting"
        link_and_build(db)
        assert not candidate_readiness(tmp_path)["ready"]


@pytest.mark.parametrize("when", ["after_snapshot", "during_read"])
def test_input_changes_during_processing_cannot_be_sealed_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        record = attach(db, blobs, "a", b"healthy")
        changed = False

        def change_input_once() -> None:
            nonlocal changed
            if not changed:
                changed = True
                with db.transaction():
                    db.upsert_document(record.model_copy(update={"title": "Titre changé pendant la construction"}))

        if when == "after_snapshot":
            original_documents = db.documents

            def changed_after_snapshot(key: str | None = None) -> list[DocumentRecord]:
                documents = original_documents(key)
                change_input_once()
                return documents

            monkeypatch.setattr(db, "documents", changed_after_snapshot)
        else:
            original_read = reader.read

            def changed_during_read(document_id: str, data: bytes) -> PdfContent:
                change_input_once()
                return original_read(document_id, data)

            monkeypatch.setattr(reader, "read", changed_during_read)
        services = services_for(blobs, reader)
        assert process_filings(db, services).processed == 1
        built = db.filing(record.filing_key)
        assert built is not None and "input" not in built.versions
        assert db.processing_outcome(record.filing_key) == "waiting"
        assert not candidate_readiness(tmp_path)["ready"]
        assert process_filings(db, services).processed == 1
        assert db.processing_outcome(record.filing_key) == "complete"
        link_and_build(db)
        assert candidate_readiness(tmp_path)["ready"]


def test_imported_correction_and_withdrawal_require_a_real_rebuild(tmp_path: Path) -> None:
    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        a = attach(db, blobs, "a", b"healthy-a")
        b = attach(db, blobs, "b", b"healthy-b")
        services = services_for(blobs, reader)
        process_and_link(db, services)
        with db.transaction():
            correction_id = db.add_correction(
                Correction(
                    filing_key=a.filing_key, field="substation", value="POSTE SYNTHETIQUE", author="Synthetic reviewer",
                    created_at=datetime.now(UTC),
                )
            )  # fmt: skip
        revision = db.processing_input_revision(a.filing_key)
        assert db.processing_outcome(a.filing_key) == "waiting"
        assert not candidate_readiness(tmp_path)["ready"]
        process_and_link(db, services, only={b.filing_key})
        assert not candidate_readiness(tmp_path)["ready"]
        process_and_link(db, services, only={a.filing_key})
        corrected = db.filing(a.filing_key)
        assert corrected is not None and corrected.substation == "POSTE SYNTHETIQUE"
        assert candidate_readiness(tmp_path)["ready"]
        with db.transaction():
            db.conn.execute("UPDATE corrections SET value=value,active=active WHERE id=?", (correction_id,))
        assert db.processing_input_revision(a.filing_key) == revision
        assert db.processing_outcome(a.filing_key) == "complete"
        with db.transaction():
            assert db.withdraw_correction(correction_id)
        assert db.processing_outcome(a.filing_key) == "waiting"
        process_and_link(db, services, only={a.filing_key})
        uncorrected = db.filing(a.filing_key)
        assert uncorrected is not None and uncorrected.substation is None
        with db.transaction():
            db.conn.execute("UPDATE corrections SET active=1 WHERE id=?", (correction_id,))
        assert db.processing_outcome(a.filing_key) == "waiting"
        process_and_link(db, services, only={a.filing_key})
        with db.transaction():
            db.conn.execute("DELETE FROM corrections WHERE id=?", (correction_id,))
        assert db.processing_outcome(a.filing_key) == "waiting"
        assert not candidate_readiness(tmp_path)["ready"]
        process_and_link(db, services, only={a.filing_key})
        assert candidate_readiness(tmp_path)["ready"]


def test_linking_input_triggers_preserve_state_and_ignore_identical_updates(tmp_path: Path) -> None:
    def dirty(db: Database) -> bool:
        return (
            db.conn.execute("SELECT 1 FROM pipeline_state WHERE name='linking_dirty' AND data='true'").fetchone()
            is not None
        )

    blobs, reader = BlobStore(tmp_path / "blobs"), SyntheticReader()
    with Database(tmp_path / "radar.sqlite") as db:
        a = attach(db, blobs, "a", b"healthy-a")
        b = attach(db, blobs, "b", b"healthy-b")
        services = services_for(blobs, reader)
        process_and_link(db, services)
        previous = db.get_pipeline_state("linking")
        assert previous is not None and not dirty(db)
        filing = db.filing(a.filing_key)
        assert filing is not None
        with db.transaction():
            db.upsert_filing(filing)
        assert not dirty(db)
        with db.transaction():
            db.upsert_filing(filing.model_copy(update={"title": "A changed filing title"}))
        assert dirty(db) and db.get_pipeline_state("linking") == previous
        assert not candidate_readiness(tmp_path)["ready"]
        link_and_build(db)
        assert not dirty(db)
        signal = Signal(
            key="company:synthetic", kind=SignalKind.COMPANY, source=SourceKind.COMPANY_REGISTRY,
            title="Synthetic company", url="https://example.test/company",
        )  # fmt: skip
        with db.transaction():
            db.upsert_signal(signal)
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            db.upsert_signal(signal)
        assert not dirty(db)
        with db.transaction():
            db.upsert_signal(signal.model_copy(update={"title": "Changed company title"}))
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            db.conn.execute("DELETE FROM signals WHERE key=?", (signal.key,))
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            decision_id = db.add_link_decision(
                LinkDecision(
                    a=a.filing_key, b=b.filing_key, verdict="unrelated", author="Synthetic reviewer",
                    created_at=datetime.now(UTC),
                )
            )  # fmt: skip
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            db.conn.execute("UPDATE link_decisions SET verdict=verdict WHERE id=?", (decision_id,))
        assert not dirty(db)
        with db.transaction():
            db.conn.execute("UPDATE link_decisions SET verdict='related' WHERE id=?", (decision_id,))
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            db.conn.execute("DELETE FROM link_decisions WHERE id=?", (decision_id,))
        assert dirty(db)
        link_and_build(db)
        with db.transaction():
            db.conn.execute("DELETE FROM filings WHERE key=?", (b.filing_key,))
        assert dirty(db) and db.get_pipeline_state("linking") is not None
