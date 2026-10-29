"""Convergent model processing: wait for durable work, not a fixed number of stages."""

import time
from dataclasses import asdict
from typing import Any

from radar.pipeline import llm_batch
from radar.pipeline.filings import Services
from radar.pipeline.run import process_filings
from radar.store.db import Database


def execute_processing(
    db: Database,
    services: Services,
    *,
    client: Any = None,
    only: set[str] | None = None,
    full: bool = False,
    wait: bool = False,
    timeout_s: float = 3600,
    poll_s: float = 10,
) -> dict[str, Any]:
    """A complete run has integrated every stage and has no blocked or uncertain request.

    Pending scheduled batches are a provisional outcome. A timed out waiter leaves its durable
    work in place for a future command; it does not cancel or resubmit provider work blindly.
    """
    if timeout_s <= 0 or poll_s <= 0:
        raise ValueError("Processing timeout and poll interval must be positive")
    started, cpu_started = time.monotonic(), time.process_time()
    deadline = started + timeout_s
    run_id = db.start_run("process")
    queue = services.llm_queue
    rounds: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    try:
        while True:
            collected = llm_batch.collect(client, queue, services.llm_tasks) if client is not None else {}
            affected = set(collected.get("filings", ()))
            selected = (only | affected) if only is not None else None
            report = process_filings(db, services, only=selected, full=full)
            submitted = llm_batch.submit(client, queue) if client is not None else []
            rounds.append({"collected": collected, "processed": report.processed, "submitted": submitted})
            active = queue.active_count() if queue is not None else 0
            blocked = queue.blocked_count() if queue is not None else 0
            status = "blocked" if blocked or report.failed else "pending" if active else "complete"
            result = {**asdict(report), "status": status, "llm_batches": rounds}
            if queue is not None:
                result["llm_jobs"] = queue.summary()
            if blocked or report.failed:
                result["error"] = "Processing has failed, blocked or uncertain work; inspect the durable job status"
                break
            if not active or not wait or client is None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.update(status="pending", error="Processing wait timed out; durable work is retained")
                break
            # Even when no new batch was submitted, an earlier provider batch may still be active.
            if queue.in_flight():
                if not llm_batch.wait(client, queue, every_s=poll_s, timeout_s=remaining):
                    result.update(status="pending", error="Processing wait timed out; durable work is retained")
                    break
            else:
                time.sleep(min(poll_s, remaining))
            full = False
            # The next iteration always collects ended work before it decides to finish.
    except Exception as exc:
        db.finish_run(run_id, {"failed": {"run": f"{type(exc).__name__}: {exc}"}, "status": "failed"})
        raise
    result["duration_s"] = round(time.monotonic() - started, 4)
    result["cpu_s"] = round(time.process_time() - cpu_started, 4)
    db.finish_run(run_id, result)
    return result
