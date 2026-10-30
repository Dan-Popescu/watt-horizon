"""API diagnostics identify their exact inputs and never recompute heavy graphs on demand."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from radar.api import quality
from radar.quality.documentary_metrics import read_quality_loop, refresh_quality_loop
from radar.store import diagnostics
from radar.store.db import Database
from tests.test_incremental_linking import filing


def test_unavailable_snapshot_is_explicit_and_read_only(db: Database, tmp_path: Path) -> None:
    before = db.conn.total_changes
    result = quality.quality_overview(db, tmp_path / "cases")
    assert result["snapshot"]["status"] == "unavailable"
    assert result["snapshot"]["computed_at"] is None and result["queue"]["size"] is None
    assert db.conn.total_changes == before


def test_snapshot_read_never_runs_heavy_builder(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = quality.refresh_quality_overview(db, tmp_path / "cases")
    assert result["snapshot"]["status"] == "current"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Diagnostic rebuilt during API read")

    monkeypatch.setattr(quality, "_build_quality_overview", forbidden)
    monkeypatch.setattr(db, "filings", forbidden)
    report = quality.quality_overview(db, tmp_path / "cases")
    assert report["snapshot"]["status"] == "current"
    assert report["snapshot"]["scope"] == {"region": None, "kind": "overview"}
    assert report["snapshot"]["computed_at"]


def test_dataset_change_marks_snapshot_stale_without_hiding_old_date(db: Database) -> None:
    result = refresh_quality_loop(db)
    with db.transaction():
        db.upsert_filing(filing("a", "Alpha", "Méru", "60000"))
    report = read_quality_loop(db)
    assert report["snapshot"]["status"] == "stale"
    assert report["snapshot"]["computed_at"] == result["snapshot"]["computed_at"]
    assert "dataset_changed" in report["snapshot"]["stale_reasons"]


def test_changed_external_cases_and_expired_snapshot_are_visible(db: Database, tmp_path: Path) -> None:
    path = tmp_path / "cases"
    path.mkdir()
    quality.refresh_quality_overview(db, path)
    (path / "case.json").write_text("{}", encoding="utf-8")
    assert "external_inputs_changed" in quality.quality_overview(db, path)["snapshot"]["stale_reasons"]
    report = diagnostics.read(
        db,
        "overview",
        scope={"region": None, "kind": "overview"},
        now=datetime.now(UTC) + timedelta(days=2),
    )
    assert "expired" in report["snapshot"]["stale_reasons"]


def test_changed_database_during_computation_cannot_publish_stale_snapshot(db: Database) -> None:
    diagnostics.refresh(db, "test", lambda: {"value": "old"}, scope={})

    def build():
        with Database(db.path) as other, other.transaction():
            other.upsert_filing(filing("a", "Alpha", "Méru", "60000"))
        return {"value": "stale"}

    with pytest.raises(ValueError, match="Dataset changed"):
        diagnostics.refresh(db, "test", build, scope={})
    report = diagnostics.read(db, "test", scope={})
    assert report["value"] == "old" and report["snapshot"]["status"] == "stale"


def test_failed_computation_keeps_previous_snapshot(db: Database) -> None:
    diagnostics.refresh(db, "test", lambda: {"value": "old"}, scope={})

    def fail():
        raise RuntimeError("Synthetic computation failure")

    with pytest.raises(RuntimeError, match="computation failure"):
        diagnostics.refresh(db, "test", fail, scope={})
    assert not db.conn.in_transaction
    assert diagnostics.read(db, "test", scope={})["value"] == "old"
