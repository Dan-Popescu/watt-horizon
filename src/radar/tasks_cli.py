"""Headless task planning and workers, separate from the read-only web service."""

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from radar.config import Settings
from radar.net.http import PoliteHttp
from radar.pipeline.fingerprint import files_fingerprint
from radar.pipeline.run import assign_partitions
from radar.pipeline.tasks import TaskWorker, application_handlers, locked, plan_pipeline, seed_pending_filings
from radar.store.db import Database
from radar.store.locking import process_lock
from radar.store.tasks import TaskLimits, TaskSpec, TaskStore
from radar.wiring import make_http, make_services


def add_parsers(sub: Any) -> None:
    parser = sub.add_parser(
        "tasks",
        help="persistent bounded pipeline work (no paid model submission)",
        epilog="Examples: radar tasks plan --generation pilot-20261030; radar tasks run --max-tasks 20",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "enqueue"):
        child = actions.add_parser(
            action,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=f"Examples:\n  radar tasks {action} --generation pilot-20261030\n"
            f"  radar tasks {action} --generation pilot-20261030 --spec /private/tasks.json",
        )
        child.add_argument("--generation", required=True, help="explicit immutable collection/processing generation")
        child.add_argument("--spec", type=Path, help="JSON array of additional TaskSpec objects (explore/download)")
        child.add_argument("--max-attempts", type=int, default=3)
        child.add_argument(
            "--max-requests", type=int, default=100, help="physical HTTP attempts, including redirects/retries"
        )
        child.add_argument("--lease-seconds", type=float, default=300)
        child.add_argument("--runtime-seconds", type=float, default=900, help="cumulative reserved/executed wall time")
        child.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    run = actions.add_parser("run", epilog="Examples: RADAR_OFFLINE=1 radar tasks run --max-tasks 20")
    run.add_argument("--max-tasks", type=int, default=100)
    run.add_argument("--worker", default="local")
    status = actions.add_parser("status", epilog="Examples: radar tasks status; radar tasks status --key TASK_KEY")
    status.add_argument("--key", help="include immutable inputs, budget and attempt history for one task")


def _limits(args: argparse.Namespace) -> TaskLimits:
    return TaskLimits(args.max_attempts, args.max_requests, args.lease_seconds, args.runtime_seconds, args.max_bytes)


def _additional(
    args: argparse.Namespace,
    *,
    collector_policy: str,
    normalize_collect: Callable[[dict[str, Any], str], dict[str, Any]] | None,
) -> list[TaskSpec]:
    if args.spec is None:
        return []
    if args.spec.stat().st_size > 1024 * 1024:
        raise ValueError("Task specification exceeds 1 MiB")
    rows = json.loads(args.spec.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Task specification must contain an array of at most 1000 tasks")
    tasks = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {"explore", "download"}:
            raise ValueError("Additional task kinds are explore or download")
        inputs = row.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("Each task requires explicit immutable inputs")
        if row["kind"] == "explore":
            if normalize_collect is None:
                raise ValueError("Collector input normalization is unavailable")
            inputs = normalize_collect(inputs, str(row.get("source", "")))
        spec = TaskSpec(
            str(row["kind"]),
            str(row["identity"]),
            {**inputs, "generation": args.generation},
            tuple(row.get("dependencies", ())),
            str(row.get("source", "")),
            True,
            _limits(args),
            policy=collector_policy if row["kind"] == "explore" else "",
        )
        if not spec.source:
            raise ValueError("Network tasks require a source identity for persistent source suspension")
        tasks.append(spec)
    return tasks


def command(
    args: argparse.Namespace,
    settings: Settings,
    *,
    collect: Callable[[TaskSpec, Database, PoliteHttp], Any] | None = None,
    normalize_collect: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with Database(settings.db_path) as db:
        store = TaskStore(db.conn)
        if args.action == "status":
            result = store.summary()
            if args.key:
                result["task"] = store.get(args.key)
                result["history"] = [
                    dict(row)
                    for row in db.conn.execute(
                        "SELECT * FROM task_attempts WHERE task_key=? ORDER BY fence", (args.key,)
                    )
                ]
            return result
        # This worker can consume model caches and durable reconciled answers;
        # paid submission remains exclusively owned by the existing model ledger.
        paths = (
            settings.gold_path,
            settings.rules_path,
            settings.aliases_path,
            settings.templates_path,
            settings.screen_terms_path,
            settings.communes_path,
            settings.catalog_path,
        )

        code_paths = tuple(sorted(Path(__file__).parent.rglob("*.py")))

        def steering_token() -> str:
            return files_fingerprint((*paths, *code_paths))

        baseline = steering_token()
        services = make_services(settings, None, llm="cached")
        collector_policy = files_fingerprint((settings.catalog_path, settings.screen_terms_path, *code_paths))
        if steering_token() != baseline:
            raise ValueError("Steering files changed while preparing task services; retry the plan")
        if args.action in {"plan", "enqueue"}:
            extra = _additional(args, collector_policy=collector_policy, normalize_collect=normalize_collect)
            if args.action == "enqueue":
                with process_lock(settings.data_dir):
                    seed_pending_filings(db)
                    assign_partitions(db, services.partitions_path, services.partitions_test_after)
            tasks = extra + plan_pipeline(
                db,
                services,
                generation=args.generation,
                limits=_limits(args),
                dependencies=tuple(task.key for task in extra),
            )
            added = 0
            if args.action == "enqueue":
                with locked(db):
                    if steering_token() != baseline:
                        raise ValueError("Steering files changed while planning; enqueue a fresh plan")
                    for task in tasks:
                        added += store.enqueue(task, now=time.time())
            return {
                "generation": args.generation,
                "tasks": [{"key": task.key, **asdict(task)} for task in tasks],
                "enqueued": added,
                "network_tasks": len(extra),
                "paid_calls": False,
            }
        if args.action != "run":
            raise ValueError("Unknown tasks action")
        http: PoliteHttp | None = make_http(settings)
        try:

            def collector(spec: TaskSpec, stage: Database) -> Any:
                if collect is None or http is None:
                    raise ValueError("Task collector is unavailable")
                return collect(spec, stage, http)

            handlers = application_handlers(
                db,
                services,
                http=http,
                collect=collector,
                input_guard=lambda: steering_token() == baseline,
                collector_policy=collector_policy,
            )
            # Discovery/download may add documents absent from the initial plan.
            # Add their downstream work after the fenced publication, never from
            # a private snapshot that might subsequently be discarded.
            worker = TaskWorker(db, handlers, offline=settings.offline, http=http, worker=args.worker)
            work = []
            if args.max_tasks < 1:
                raise ValueError("Worker task budget must be positive")
            for _ in range(args.max_tasks):
                outcome = worker.run_one()
                if outcome is None:
                    break
                work.append(outcome)
                current = store.get(outcome["key"])
                if outcome["state"] == "complete" and current and current["kind"] in {"explore", "download"}:
                    with process_lock(settings.data_dir):
                        seed_pending_filings(db)
                        assign_partitions(db, services.partitions_path, services.partitions_test_after)
                    downstream = plan_pipeline(db, services, generation=outcome["key"], dependencies=(outcome["key"],))
                    with locked(db):
                        for spec in downstream:
                            store.enqueue(spec, now=time.time())
            return {"work": work, **store.summary(), "offline": settings.offline, "paid_calls": False}
        finally:
            if http is not None:
                http.close()
