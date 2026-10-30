"""Crash, lease and atomic publication contracts; no remote service is contacted."""

import time
from dataclasses import replace
from datetime import date

import httpx
import pytest

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.cli import build_parser
from radar.config import Settings
from radar.domain.enums import Category, SourceKind
from radar.net.http import PoliteHttp
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.tasks import TaskWorker, application_handlers, locked, plan_pipeline, prepare_snapshot
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.store.readiness import candidate_readiness
from radar.store.tasks import TaskLeaseLostError, TaskLimits, TaskSpec, TaskStore
from radar.tasks_cli import command
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, FORM_FIELDS, make_pdf


def test_crash_expiration_and_fencing(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("test", "a", {}, limits=TaskLimits(lease_s=5))
    with locked(db):
        assert store.enqueue(spec, now=10)
        first = store.claim("crashed", now=10, offline=True)
    assert first
    # New process/connection sees the uncompleted reservation.
    with Database(db.path) as second:
        with locked(second):
            lease = TaskStore(second.conn).claim("restarted", now=16, offline=True)
        assert lease and lease.fence > first.fence
        with locked(second), pytest.raises(TaskLeaseLostError):
            TaskStore(second.conn).check(first, now=16)
        with locked(second):
            TaskStore(second.conn).finish(lease, "complete", now=17, result={"ok": True})
    assert store.get(spec.key)["attempts"] == 2  # type: ignore[index]
    assert [row[0] for row in db.conn.execute("SELECT state FROM task_attempts ORDER BY fence")] == [
        "expired",
        "complete",
    ]


def test_immutable_replacement_dedup_and_dependencies(db: Database) -> None:
    store = TaskStore(db.conn)
    old = TaskSpec("read", "document", {"sha": "old"})
    with locked(db):
        assert store.enqueue(old, now=0)
        assert not store.enqueue(old, now=0)
        lease = store.claim("old-worker", now=0, offline=True)
        assert lease
        newer = replace(old, inputs={"sha": "new"})
        store.enqueue(newer, now=1)
        child = TaskSpec("extract", "filing", {}, (old.key,))
        store.enqueue(child, now=1)
        store.claim("new-worker", now=1, offline=True)
        assert store.get(child.key)["state"] == "blocked"  # type: ignore[index]
        with pytest.raises(TaskLeaseLostError):
            store.finish(lease, "complete", now=1)


def test_offline_network_wait_does_not_consume_attempt(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("download", "url", {"generation": "1"}, network=True, source="dreal")
    with locked(db):
        store.enqueue(spec, now=0)
        assert store.claim("offline", now=10, offline=True) is None
    assert store.get(spec.key)["state"] == "pending"  # type: ignore[index]
    assert store.get(spec.key)["attempts"] == 0  # type: ignore[index]


def test_atomic_publication_and_input_revision(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("work", "one", {})
    with locked(db):
        store.enqueue(spec, now=0)

    def compute(_: object):
        def operation(stage: Database):
            with stage.transaction():
                stage.set_pipeline_state("published", {"yes": True})
            return {"yes": True}

        result = prepare_snapshot(db, operation)
        # A human edit between computation and publication invalidates the whole result.
        with db.transaction():
            db.set_pipeline_state("human-change", {"revision": 1})
        return result

    outcome = TaskWorker(db, {"work": compute}, offline=True, clock=lambda: 1).run_one()
    assert outcome and outcome["state"] == "superseded"
    assert db.get_pipeline_state("published") is None
    assert db.get_pipeline_state("human-change") == {"revision": 1}


def test_publication_failure_rolls_back_all_rows_and_completion(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("work", "one", {}, limits=TaskLimits(max_attempts=1))
    with locked(db):
        store.enqueue(spec, now=0)

    def compute(_: object):
        prepared = prepare_snapshot(db, lambda stage: stage.set_pipeline_state("partial", {"wrong": True}))
        original = prepared.publish

        def fail(destination: Database):
            original(destination)
            raise RuntimeError("publication interrupted")

        prepared.publish = fail
        return prepared

    result = TaskWorker(db, {"work": compute}, offline=True, clock=lambda: 1).run_one()
    assert result and result["state"] == "failed"
    assert db.get_pipeline_state("partial") is None
    assert store.get(spec.key)["state"] == "failed"  # type: ignore[index]


def test_physical_redirect_budget_survives_worker_restart(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("work", "url", {}, network=True, source="dreal", limits=TaskLimits(max_requests=2))
    with locked(db):
        store.enqueue(spec, now=0)
    sent = []

    def response(request: httpx.Request):
        sent.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/next"})

    with PoliteHttp("test", transport=httpx.MockTransport(response), default_interval_s=0) as http:

        def compute(_: object):
            http.get_bytes("https://example.org/start")
            raise AssertionError("budget should stop the redirect")

        first = TaskWorker(db, {"work": compute}, offline=False, http=http, clock=lambda: 1).run_one()
        second = TaskWorker(db, {"work": compute}, offline=False, http=http, clock=lambda: 40).run_one()
    assert first and first["state"] == "blocked" and second is None
    assert len(sent) == 2
    assert store.get(spec.key)["requests"] == 2  # type: ignore[index]


def test_generic_python_handler_is_interrupted_at_runtime_budget(db: Database) -> None:
    spec = TaskSpec("slow", "a", {}, limits=TaskLimits(lease_s=1, max_runtime_s=0.03))
    with locked(db):
        TaskStore(db.conn).enqueue(spec, now=time.time())

    def slow(_: object):
        time.sleep(0.5)
        raise AssertionError("Deadline should interrupt this handler")

    started = time.monotonic()
    result = TaskWorker(db, {"slow": slow}, offline=True).run_one()
    assert result and result["state"] in {"blocked", "superseded"}
    assert "TaskDeadlineError" in result["error"]
    assert time.monotonic() - started < 0.4


def test_source_block_suspends_all_queued_and_running_tasks(db: Database) -> None:
    store = TaskStore(db.conn)
    source_a = TaskSpec("work", "a", {}, network=True, source="dreal")
    source_b = TaskSpec("work", "b", {}, network=True, source="dreal")
    other = TaskSpec("work", "c", {}, network=True, source="mrae")
    with locked(db):
        for spec in (source_a, source_b, other):
            store.enqueue(spec, now=0)
    with PoliteHttp("test", transport=httpx.MockTransport(lambda _: httpx.Response(403)), default_interval_s=0) as http:

        def blocked(_: object):
            http.get_bytes("https://example.org/denied")
            raise AssertionError("unreachable")

        worker = TaskWorker(db, {"work": blocked}, offline=False, http=http, clock=lambda: 1)
        # Claim a deterministic source task first, independently of the task hash sort order.
        with db.transaction():
            db.conn.execute("UPDATE pipeline_tasks SET created_at=1 WHERE key=?", (other.key,))
        result = worker.run_one()
    assert result and result["state"] == "blocked"
    assert store.get(source_a.key)["state"] == "blocked"  # type: ignore[index]
    assert store.get(source_b.key)["state"] == "blocked"  # type: ignore[index]
    assert store.get(other.key)["state"] == "pending"  # type: ignore[index]


def test_runtime_expiration_discards_prepared_result(db: Database) -> None:
    store = TaskStore(db.conn)
    spec = TaskSpec("work", "a", {}, limits=TaskLimits(lease_s=5, max_runtime_s=3))
    with locked(db):
        store.enqueue(spec, now=0)
    now = [1.0]

    def compute(_: object):
        prepared = prepare_snapshot(db, lambda stage: stage.set_pipeline_state("late", {"bad": True}))
        now[0] = 5
        return prepared

    result = TaskWorker(db, {"work": compute}, offline=True, clock=lambda: now[0]).run_one()
    assert result and result["state"] == "discarded"
    assert db.get_pipeline_state("late") is None
    with locked(db):
        assert store.claim("restart", now=6, offline=True) is None
    assert store.get(spec.key)["state"] == "failed"  # type: ignore[index]


def test_real_read_extract_and_link_handlers(
    db: Database, settings: Settings, blobs: BlobStore, lookups: StaticLookups
) -> None:
    # Verbatim field contents from the shared public-document regression fixture.
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER], FORM_FIELDS),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="010504",
            url="https://example.org/form.pdf",
            filename="cerfa.pdf",
            title="Stockage batteries",
        )
    classifier = GoldClassifier(
        {"010504": GoldLabel("010504", Category.BESS)}, known_keys=db.filing_keys_with_documents()
    )
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, lookups=lookups, fingerprint="test-policy")
    store = TaskStore(db.conn)
    planned = plan_pipeline(db, services, generation="test")
    with locked(db):
        for spec in planned:
            store.enqueue(spec, now=0)
    result = TaskWorker(db, application_handlers(db, services), offline=True, clock=lambda: 1).run(max_tasks=20)
    assert result["states"] == {"complete": 3}
    assert db.filings()[0].machine is not None
    assert db.filings()[0].power.mw == 100
    assert db.projects()
    # Re-enqueueing the exact immutable plan never duplicates work.
    with locked(db):
        assert not any(store.enqueue(spec, now=2) for spec in planned)


def test_cli_new_documents_are_frozen_prospectively_before_extraction(
    db: Database, settings: Settings, blobs: BlobStore
) -> None:
    settings = replace(settings, evaluation_test_after=date(2020, 1, 1))
    with db.transaction():
        record = ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER], FORM_FIELDS),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="010504",
            url="https://example.org/form.pdf",
            filename="cerfa.pdf",
            title="Stockage batteries",
        )
    assert not db.filings()
    parser = build_parser()
    queued = command(parser.parse_args(["tasks", "enqueue", "--generation", "prospective"]), settings)
    pending = db.filing(record.filing_key)
    assert queued["enqueued"] == 3 and pending is not None
    assert pending.strata["evaluation_partition"] == "test"
    assert pending.machine is None and pending.title == ""
    before = settings.partitions_path.read_bytes()
    result = command(parser.parse_args(["tasks", "run", "--max-tasks", "10"]), settings)
    assert result["states"] == {"complete": 3}
    assert db.filing(record.filing_key).strata["evaluation_partition"] == "test"  # type: ignore[union-attr]
    assert settings.partitions_path.read_bytes() == before
    assert candidate_readiness(settings.data_dir)["task_states"] == {"complete": 3}


def test_human_decision_change_before_link_claim_supersedes_plan(
    db: Database, blobs: BlobStore, lookups: StaticLookups
) -> None:
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=lookups)
    spec = plan_pipeline(db, services, generation="immutable")[0]
    with locked(db):
        TaskStore(db.conn).enqueue(spec, now=0)
    with db.transaction():
        db.conn.execute(
            "INSERT INTO link_decisions (a,b,verdict,author,created_at) VALUES ('a','b','same_project','reader','2026-10-30T00:00:00+00:00')"
        )
    result = TaskWorker(db, application_handlers(db, services), offline=True, clock=lambda: 1).run_one()
    assert result and result["state"] == "superseded"


def test_unfinished_pipeline_work_blocks_dataset_readiness(db: Database, settings: Settings) -> None:
    spec = TaskSpec("download", "url", {"generation": "1"}, network=True, source="dreal")
    with locked(db):
        TaskStore(db.conn).enqueue(spec, now=0)
    assert "pipeline_tasks_unfinished" in candidate_readiness(settings.data_dir)["reasons"]
