"""Recoverable local snapshots preserve committed WAL data and durable model work."""

import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from radar.domain.enums import SourceKind
from radar.domain.models import Filing
from radar.evaluation.baseline import approve_baseline, dataset_fingerprint
from radar.pipeline.operations import (
    ProcessBusyError,
    PublishedDataset,
    activate_dataset,
    active_dataset_path,
    dataset_status,
    operational_status,
    process_lock,
    publish_dataset,
)
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.store.snapshots import (
    SnapshotError,
    backup_dataset,
    restore_dataset,
)


def test_backup_restores_committed_wal_and_durable_jobs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    gold = tmp_path / "gold"
    gold.mkdir()
    (gold / "labels.jsonl").write_text('{"number":"synthetic","verified":true}\n', encoding="utf-8")
    (data / "blobs").mkdir()
    (data / "blobs" / "synthetic.pdf").write_bytes(b"synthetic-public-filing")
    with sqlite3.connect(data / "radar.sqlite") as current:
        current.execute("PRAGMA journal_mode=WAL")
        current.execute("CREATE TABLE example(value TEXT)")
        current.execute("INSERT INTO example VALUES ('committed')")
        current.commit()
        with sqlite3.connect(data / "llm-jobs.sqlite") as jobs:
            jobs.execute("CREATE TABLE jobs(provider_id TEXT)")
            jobs.execute("INSERT INTO jobs VALUES ('batch-synthetic')")
        snapshot = backup_dataset(data, tmp_path / "backups", gold_dir=gold)
        restored = tmp_path / "restored"
        restored_gold = tmp_path / "restored-gold"
        restore_dataset(Path(snapshot["path"]), restored, gold_destination=restored_gold)
    with sqlite3.connect(restored / "radar.sqlite") as recovered:
        assert recovered.execute("SELECT value FROM example").fetchall() == [("committed",)]
    with sqlite3.connect(restored / "llm-jobs.sqlite") as recovered_jobs:
        assert recovered_jobs.execute("SELECT provider_id FROM jobs").fetchall() == [("batch-synthetic",)]
    assert (restored / "blobs" / "synthetic.pdf").read_bytes() == b"synthetic-public-filing"
    assert (restored_gold / "labels.jsonl").read_bytes() == (gold / "labels.jsonl").read_bytes()


def test_restore_rejects_corruption_without_touching_destination(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "radar.sqlite") as current:
        current.execute("CREATE TABLE example(value TEXT)")
    snapshot = backup_dataset(data, tmp_path / "backups")
    snapshot_path = Path(snapshot["path"])
    with (snapshot_path / "data" / "radar.sqlite").open("ab") as target:
        target.write(b"corruption")
    destination = tmp_path / "destination"
    with pytest.raises(SnapshotError, match="checksums"):
        restore_dataset(snapshot_path, destination)
    assert not destination.exists()


def test_restore_never_overwrites_existing_work(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "radar.sqlite") as current:
        current.execute("CREATE TABLE example(value TEXT)")
    snapshot = backup_dataset(data, tmp_path / "backups")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "important.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(SnapshotError, match="empty directory"):
        restore_dataset(Path(snapshot["path"]), destination)
    assert (destination / "important.txt").read_text() == "keep me"


def test_publication_rejects_unapproved_data_without_an_active_pointer(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with Database(data / "radar.sqlite"):
        pass
    published = tmp_path / "published"
    with pytest.raises(ValueError, match="approved"):
        publish_dataset(data / "radar.sqlite", published, baseline_path=data / "evaluation" / "approved.json")
    assert not (published / "active.json").exists()
    with pytest.raises(SnapshotError, match="active"):
        active_dataset_path(published)


def candidate_report() -> dict[str, Any]:
    """A synthetic independently evaluated candidate, unrelated to production data."""
    return {
        "gold": {"labels_verified": 1},
        "screening": {"recall_on_gold": 1.0},
        "completeness": {},
        "fields_verified": {"dev": {}, "test": {"power_mw": {"n": 10, "accuracy": 1.0}}},
        "fields_draft": {},
        "blind": {},
        "field_calibration": {"ece": None, "auto_accept_share": None},
        "privacy": {"leaks": []},
        "integrity": {"quote_not_found": 0},
        "undecided": {"share": 0.0},
        "cases": {},
        "ocr": {},
        "linking": {"grouping": {}},
        "second_opinion": {},
        "partitions": {"fingerprint": "synthetic-frozen-population"},
        "reference_fingerprint": "synthetic-frozen-gold",
        "robustness": {"stable_share": 1.0},
    }


def test_publication_preserves_active_version_on_failure_and_can_roll_back(tmp_path: Path) -> None:
    data, published = tmp_path / "data", tmp_path / "published"
    approval = data / "evaluation" / "approved.json"
    with Database(data / "radar.sqlite") as db:
        approve_baseline(
            approval,
            candidate_report(),
            dataset_id=dataset_fingerprint(db.path),
            min_samples=1,
            required_fields=("power_mw",),
        )
        first = publish_dataset(db.path, published, baseline_path=approval)
        first_path = active_dataset_path(published)
        resolver = PublishedDataset(published)
        assert resolver() == first_path
        with db.transaction():
            db.upsert_filing(
                Filing(
                    key="d:h:synthetic",
                    source=SourceKind.DREAL_CASE_BY_CASE,
                    region="h",
                    number="synthetic",
                    title="Synthetic filing",
                    document_ids=(),
                )
            )
        with pytest.raises(ValueError, match="different dataset"):
            publish_dataset(db.path, published, baseline_path=approval)
        assert active_dataset_path(published) == first_path
        approve_baseline(
            approval,
            candidate_report(),
            dataset_id=dataset_fingerprint(db.path),
            min_samples=1,
            required_fields=("power_mw",),
        )
        second = publish_dataset(db.path, published, baseline_path=approval)
        assert second["version"] != first["version"]
        assert active_dataset_path(published) != first_path
        assert resolver() == Path(second["db_path"])
        activate_dataset(published, first["version"])
        assert active_dataset_path(published) == first_path
        assert resolver() == first_path
        assert dataset_status(published)["version"] == first["version"]
    with sqlite3.connect(first_path.as_uri() + "?mode=ro", uri=True) as reader:
        assert reader.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM filings")


def test_processing_lock_is_nested_but_excludes_another_writer(tmp_path: Path) -> None:
    data = tmp_path / "data"

    def other_writer() -> None:
        with process_lock(data):
            pytest.fail("Concurrent writer acquired the lease")

    with (
        process_lock(data),
        process_lock(data),
        ThreadPoolExecutor(max_workers=1) as executor,
        pytest.raises(ProcessBusyError),
    ):
        executor.submit(other_writer).result(timeout=5)
    with process_lock(data):
        pass


def test_retention_preserves_unknown_directories_and_user_files(tmp_path: Path) -> None:
    data, backups = tmp_path / "data", tmp_path / "backups"
    with Database(data / "radar.sqlite"):
        pass
    first = backup_dataset(data, backups)
    unknown = backups / "watt-snapshot-user-notes"
    unknown.mkdir()
    (unknown / "important.txt").write_text("keep me", encoding="utf-8")
    second = backup_dataset(data, backups, keep=1)
    assert not Path(first["path"]).exists()
    assert Path(second["path"]).is_dir()
    assert (unknown / "important.txt").read_text() == "keep me"


def test_status_does_not_create_database_and_separates_freshness(tmp_path: Path) -> None:
    data = tmp_path / "data"
    missing = operational_status(data)
    assert not missing["database"]["ready"]
    assert not data.exists()
    with Database(data / "radar.sqlite"):
        pass
    empty = operational_status(data)
    assert empty["database"]["ready"]
    assert empty["freshness"]["status"] == "unknown"


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires POSIX fork")
def test_forked_child_does_not_inherit_reentrant_ownership(tmp_path: Path) -> None:
    data = tmp_path / "data"

    def competing_child() -> None:
        try:
            with process_lock(data):
                pass
        except ProcessBusyError:
            raise SystemExit(17) from None

    with process_lock(data):
        process = multiprocessing.get_context("fork").Process(target=competing_child)
        process.start()
        process.join(timeout=5)
        try:
            assert process.exitcode == 17
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_backup_preserves_unmigrated_paid_work_without_copying_ocr_cache(tmp_path: Path) -> None:
    data = tmp_path / "data"
    with Database(data / "radar.sqlite"):
        pass
    cache = KeyValueCache(data / "cache" / "derived.sqlite")
    cache.put("llm-batches", "batch-synthetic", {"entries": {"synthetic": {"filing": "synthetic"}}})
    cache.put("pdf", "large-derived-result", {"text": "rebuildable"})
    cache.close()
    snapshot = backup_dataset(data, tmp_path / "backups")
    restored = tmp_path / "restored"
    restore_dataset(Path(snapshot["path"]), restored)
    with sqlite3.connect((restored / "cache" / "derived.sqlite").as_uri() + "?mode=ro", uri=True) as recovered:
        rows = recovered.execute("SELECT namespace, key FROM kv").fetchall()
    assert rows == [("llm-batches", "batch-synthetic")]


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires POSIX fork")
def test_crashed_writer_does_not_leave_a_stale_lease(tmp_path: Path) -> None:
    data = tmp_path / "data"

    def crash_inside_lease() -> None:
        with process_lock(data):
            os._exit(23)  # Simulate process death without Python finally blocks.

    process = multiprocessing.get_context("fork").Process(target=crash_inside_lease)
    process.start()
    process.join(timeout=5)
    try:
        assert process.exitcode == 23
        with process_lock(data):
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
