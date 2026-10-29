"""A completed evaluation never hides an unfinished or interrupted processing run."""

from pathlib import Path

import pytest

from radar.classification.base import ClassificationInput
from radar.classification.llm import LlmClassifier
from radar.evaluation.baseline import approve_baseline, dataset_fingerprint
from radar.pipeline.llm_batch import LlmQueue
from radar.pipeline.operations import active_dataset_path, publish_dataset
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.store.readiness import candidate_readiness
from radar.store.snapshots import SnapshotError
from tests.test_operations import candidate_report


def test_blocked_and_crashed_runs_are_not_ready(tmp_path: Path) -> None:
    with Database(tmp_path / "radar.sqlite") as db:
        run = db.start_run("process")
        assert candidate_readiness(tmp_path)["ready"] is False
        db.finish_run(run, {"status": "blocked", "failed": {}, "error": "budget exhausted"})
        readiness = candidate_readiness(tmp_path)
        assert readiness["ready"] is False
        assert "process_blocked" in readiness["reasons"]
        run = db.start_run("process")
        db.finish_run(run, {"status": "complete", "failed": {}})
        assert candidate_readiness(tmp_path)["ready"] is True


def test_pending_work_prevents_republishing_an_old_approval(tmp_path: Path) -> None:
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
        publish_dataset(db.path, published, baseline_path=approval)
        original = active_dataset_path(published)
        cache = KeyValueCache(data / "cache" / "derived.sqlite")
        queue = LlmQueue(cache, jobs_path=data / "llm-jobs.sqlite")
        try:
            task = LlmClassifier("synthetic-no-network")
            item = ClassificationInput("d:h:synthetic", "synthetic", "Synthetic", "battery project")
            queue.add(task, item, item.filing_key)
            readiness = candidate_readiness(data)
            assert readiness["job_states"] == {"pending": 1}
            assert readiness["ready"] is False
            with pytest.raises(SnapshotError, match="incomplete"):
                publish_dataset(db.path, published, baseline_path=approval)
            assert active_dataset_path(published) == original
        finally:
            queue.jobs.close()
            cache.close()
