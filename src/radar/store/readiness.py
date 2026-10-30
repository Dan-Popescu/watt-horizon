"""Read-only evidence that a candidate has no unfinished processing or paid work."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def candidate_readiness(data_dir: Path, *, verify_inputs: bool = True) -> dict[str, Any]:
    """History and jobs can become incomplete without changing any project row.

    Read actual durable state, including a crash before the run could finish. Status reads
    never create a database or treat an unavailable journal as proof of completion.
    """
    reasons: list[str] = []
    states: dict[str, int] = {}
    task_states: dict[str, int] = {}
    path = data_dir / "radar.sqlite"
    if not path.is_file():
        return {"ready": False, "reasons": ["dataset_missing"], "job_states": states}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_tasks'"
            ).fetchone():
                task_states = {
                    str(state): int(count)
                    for state, count in connection.execute("SELECT state,COUNT(*) FROM pipeline_tasks GROUP BY state")
                }
                if any(count and state not in {"complete", "superseded"} for state, count in task_states.items()):
                    reasons.append("pipeline_tasks_unfinished")
            row = connection.execute(
                "SELECT finished_at, stats FROM runs WHERE kind='process' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                stats = json.loads(row[1] or "{}")
                status = stats.get("status")
                if row[0] is None:
                    reasons.append("process_interrupted_or_running")
                elif status and status != "complete":
                    reasons.append(f"process_{status}")
                if stats.get("failed") or stats.get("error"):
                    reasons.append("process_failed_or_incomplete")
            if connection.execute("SELECT 1 FROM processing_outcomes WHERE state != 'complete' LIMIT 1").fetchone():
                reasons.append("filing_processing_unfinished")
            if connection.execute("SELECT 1 FROM pipeline_state WHERE name='linking_dirty'").fetchone():
                reasons.append("project_rebuild_required")
            if verify_inputs:
                if connection.execute(
                    "SELECT 1 FROM document_attachments d LEFT JOIN filings f ON f.key=d.filing_key "
                    "WHERE f.key IS NULL OR NOT EXISTS ("
                    "SELECT 1 FROM json_each(f.data, '$.document_ids') ids WHERE ids.value=d.document_id) LIMIT 1"
                ).fetchone():
                    reasons.append("documents_without_current_filing")
                if connection.execute(
                    "SELECT 1 FROM filings f, json_each(f.data, '$.document_ids') ids WHERE NOT EXISTS ("
                    "SELECT 1 FROM document_attachments d WHERE d.filing_key=f.key AND d.document_id=ids.value) LIMIT 1"
                ).fetchone():
                    reasons.append("filing_documents_changed")
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        reasons.append("processing_status_unavailable")
    jobs = data_dir / "llm-jobs.sqlite"
    if jobs.is_file():
        try:
            with closing(sqlite3.connect(jobs.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                states = {
                    str(state): int(count)
                    for state, count in connection.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state")
                }
                if any(count and state != "succeeded" for state, count in states.items()):
                    reasons.append("model_jobs_unfinished")
                if connection.execute(
                    "SELECT 1 FROM submissions WHERE state IN ('submitting','in_flight','uncertain') LIMIT 1"
                ).fetchone():
                    reasons.append("model_submission_unfinished")
        except sqlite3.Error:
            reasons.append("model_status_unavailable")
    else:
        legacy = data_dir / "cache" / "derived.sqlite"
        if legacy.is_file():
            try:
                with closing(sqlite3.connect(legacy.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    if connection.execute(
                        "SELECT 1 FROM kv WHERE namespace IN ('llm-pending','llm-batches') LIMIT 1"
                    ).fetchone():
                        reasons.append("legacy_model_jobs_require_migration")
            except sqlite3.Error:
                reasons.append("legacy_model_status_unavailable")
    return {
        "ready": not reasons,
        "reasons": sorted(set(reasons)),
        "job_states": states,
        "task_states": task_states,
        "inputs_verified": verify_inputs,
    }
