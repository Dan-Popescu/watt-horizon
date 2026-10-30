"""Fenced workers over existing operations, computed against private SQLite snapshots.

The database delta is prepared on disk before publication. Content-addressed blobs
and disposable caches may be populated during computation; only a fenced atomic
publication can make their results part of the working dataset.
"""

import hashlib
import signal
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from radar.domain.models import DocumentRecord, Filing, Link
from radar.evaluation.partitions import build_manifest, load_manifest
from radar.net.http import PoliteHttp, RequestBudgetError, ResponseLimitError, SiteBlockedError
from radar.pipeline.filings import Services, read_documents
from radar.pipeline.fingerprint import corrections_fingerprint, digest, processing_fingerprint
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import CachedOcr, ChainOcr, OcrEngine, TranscriptOcr
from radar.reading.secondary import SelectiveOcr
from radar.store.db import Database
from radar.store.tasks import TaskLease, TaskLeaseLostError, TaskLimits, TaskSpec, TaskState, TaskStore, canonical


class TaskBlockedError(RuntimeError):
    """External reconciliation or operator action is required, never a blind retry."""

    def __init__(self, message: str, result: Any = None) -> None:
        super().__init__(message)
        self.result = result


class TaskInputsChangedError(RuntimeError):
    """An immutable queued input no longer denotes the current application input."""


class TaskDeadlineError(RuntimeError):
    """A generic worker exceeded its admitted wall-time budget."""


@contextmanager
def computation_deadline(seconds: float) -> Iterator[None]:
    """Interrupt Python work on POSIX; native readers have their own child watchdog.

    A CLI worker runs on the main thread. Unsupported runtimes fail closed rather
    than advertising a deadline they cannot enforce. This is not a memory limit;
    the worker's container/process limit remains necessary.
    """
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        raise TaskBlockedError("Generic task workers require a POSIX main-thread deadline timer")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signum: int, _frame: Any) -> None:
        raise TaskDeadlineError("Task computation exceeded its lease/runtime wall-time budget")

    signal.signal(signal.SIGALRM, expired)
    limit = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds
    signal.setitimer(signal.ITIMER_REAL, max(0.001, limit))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1]
            )


@contextmanager
def locked(db: Database) -> Iterator[None]:
    """No network, parser or expensive operation belongs inside this transaction."""
    if db.conn.in_transaction:
        raise RuntimeError("Task coordination requires its own short transaction")
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.conn.rollback()
        raise
    else:
        db.conn.commit()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'task_%' AND name NOT IN ('pipeline_tasks','diagnostic_revision') ORDER BY name"
        )
    ]


@dataclass
class PreparedWork:
    revision: int
    result: Any
    publish: Callable[[Database], None]
    cleanup: Callable[[], None] = lambda: None
    state: TaskState = "complete"
    error: str | None = None
    validate: Callable[[], bool] = lambda: True


def prepare_snapshot(db: Database, operation: Callable[[Database], Any]) -> PreparedWork:
    """Run unchanged collectors/builders away from the live DB, then stage SQL deltas.

    Snapshot freshness uses a migration-maintained revision, rather than hashing
    a whole dataset under a write lock. Row differences and archive parsing happen
    before publication. Trigger-maintained derived rows are restored last.
    """
    temporary = tempfile.TemporaryDirectory(prefix="radar-task-")
    root = Path(temporary.name)
    base_path, stage_path = root / "base.sqlite", root / "stage.sqlite"
    try:
        with sqlite3.connect(base_path) as target:
            db.conn.backup(target)
        with sqlite3.connect(base_path) as source, sqlite3.connect(stage_path) as target:
            source.backup(target)
        with Database(stage_path) as stage:
            revision = TaskStore(stage.conn).revision()
            result = operation(stage)
            stage.conn.commit()
            tables = _tables(stage.conn)
            stage.conn.execute("ATTACH DATABASE ? AS original", (str(base_path),))
            deltas: list[tuple[str, list[str], list[str], str, str]] = []
            for index, table in enumerate(tables):
                columns = list(stage.conn.execute(f"PRAGMA table_info({_quote(table)})"))
                names = [str(column[1]) for column in columns]
                primary = [str(column[1]) for column in sorted(columns, key=lambda c: c[5]) if column[5]]
                if not primary:
                    raise RuntimeError(f"Task publication requires a primary key: {table}")
                equal = " AND ".join(f"old.{_quote(n)} IS new.{_quote(n)}" for n in names)
                same_key = " AND ".join(f"old.{_quote(n)} IS new.{_quote(n)}" for n in primary)
                up, remove = f"task_delta_up_{index}", f"task_delta_remove_{index}"
                stage.conn.execute(
                    f"CREATE TABLE {_quote(up)} AS SELECT new.* FROM main.{_quote(table)} new "
                    f"WHERE NOT EXISTS (SELECT 1 FROM original.{_quote(table)} old WHERE {equal})"
                )
                stage.conn.execute(
                    f"CREATE TABLE {_quote(remove)} AS SELECT old.* FROM original.{_quote(table)} old "
                    f"WHERE NOT EXISTS (SELECT 1 FROM main.{_quote(table)} new WHERE {same_key})"
                )
                if (
                    stage.conn.execute(f"SELECT 1 FROM {_quote(up)} LIMIT 1").fetchone()
                    or stage.conn.execute(f"SELECT 1 FROM {_quote(remove)} LIMIT 1").fetchone()
                ):
                    deltas.append((table, names, primary, up, remove))
            stage.conn.commit()

        # The attached file is private, immutable after preparation and never a user URL.
        def publish(destination: Database) -> None:
            destination.conn.execute("PRAGMA defer_foreign_keys=ON")
            final = {"processing_outcomes": 1, "dataset_state": 2, "pipeline_state": 3}
            ordered = sorted(deltas, key=lambda item: (final.get(item[0], 0), item[0]))
            for table, names, primary, up, remove in ordered:
                same_key = " AND ".join(f"gone.{_quote(n)} IS {_quote(table)}.{_quote(n)}" for n in primary)
                destination.conn.execute(
                    f"DELETE FROM {_quote(table)} WHERE EXISTS (SELECT 1 FROM task_stage.{_quote(remove)} gone WHERE {same_key})"
                )
                columns = ",".join(map(_quote, names))
                updates = ",".join(f"{_quote(n)}=excluded.{_quote(n)}" for n in names if n not in primary)
                action = "DO UPDATE SET " + updates if updates else "DO NOTHING"
                destination.conn.execute(
                    f"INSERT INTO {_quote(table)} ({columns}) SELECT {columns} FROM task_stage.{_quote(up)} WHERE 1 "
                    f"ON CONFLICT ({','.join(map(_quote, primary))}) {action}"
                )

        # Attach before taking the publication lock; DETACH follows commit/rollback.
        db.conn.execute("ATTACH DATABASE ? AS task_stage", (str(stage_path),))

        def cleanup() -> None:
            db.conn.execute("DETACH DATABASE task_stage")
            temporary.cleanup()

        return PreparedWork(revision, result, publish, cleanup)
    except BaseException:
        temporary.cleanup()
        raise


Handler = Callable[[TaskLease], PreparedWork]


class TaskWorker:
    def __init__(
        self,
        db: Database,
        handlers: dict[str, Handler],
        *,
        offline: bool,
        http: PoliteHttp | None = None,
        clock: Callable[[], float] = time.time,
        worker: str = "local",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db, self.handlers, self.offline, self.http = db, handlers, offline, http
        self.clock, self.worker = clock, worker
        self.sleep = sleep
        self.store = TaskStore(db.conn)

    def _request(self, lease: TaskLease) -> None:
        # Conservative global pacing survives task boundaries and process restarts.
        # Waiting happens outside the write lock; admission is counted before send.
        while True:
            with locked(self.db):
                current = self.clock()
                self.store.check(lease, now=current)
                allowed = float(
                    self.db.conn.execute("SELECT next_allowed FROM task_network_admission WHERE id=1").fetchone()[0]
                )
                if current >= allowed:
                    self.store.request(lease, now=current)
                    self.db.conn.execute("UPDATE task_network_admission SET next_allowed=? WHERE id=1", (current + 5,))
                    return
                delay = allowed - current
            if allowed >= lease.until:
                raise TaskDeadlineError("Persistent HTTP pacing would exceed the task lease")
            self.sleep(delay)

    def _check_send(self, lease: TaskLease) -> None:
        with locked(self.db):
            self.store.check(lease, now=self.clock())

    def run_one(self) -> dict[str, Any] | None:
        with locked(self.db):
            lease = self.store.claim(self.worker, now=self.clock(), offline=self.offline)
        if lease is None:
            return None
        prepared = None
        try:
            if lease.spec.kind not in self.handlers:
                raise TaskBlockedError(f"No worker handler for task kind: {lease.spec.kind}")
            if lease.spec.network:
                if self.http is None:
                    raise TaskBlockedError("Network task requires the configured PoliteHttp client")
                previous = self.store.get(lease.key)
                remaining = lease.spec.limits.max_requests - int((previous or {}).get("requests", 0))
                previous_bytes, previous_seconds = self.http.max_response_bytes, self.http.max_response_seconds
                try:
                    self.http.max_response_bytes = min(previous_bytes, lease.spec.limits.max_bytes)
                    self.http.max_response_seconds = min(previous_seconds, max(0.001, lease.until - self.clock()))
                    with (
                        computation_deadline(lease.until - self.clock()),
                        self.http.request_budget(
                            remaining,
                            on_request=lambda: self._request(lease),
                            before_send=lambda: self._check_send(lease),
                        ),
                    ):
                        prepared = self.handlers[lease.spec.kind](lease)
                finally:
                    self.http.max_response_bytes, self.http.max_response_seconds = previous_bytes, previous_seconds
            else:
                with computation_deadline(lease.until - self.clock()):
                    prepared = self.handlers[lease.spec.kind](lease)
            with locked(self.db):
                self.store.check(lease, now=self.clock())
                if self.store.revision() != prepared.revision or not prepared.validate():
                    raise TaskInputsChangedError("Dataset changed while the private snapshot was being computed")
                prepared.publish(self.db)
                self.store.finish(lease, prepared.state, now=self.clock(), result=prepared.result, error=prepared.error)
            return {"key": lease.key, "state": prepared.state, "result": prepared.result}
        except SiteBlockedError as exc:
            with locked(self.db):
                self.store.block_source(lease.spec.source, str(exc), now=self.clock())
            return {"key": lease.key, "state": "blocked", "error": str(exc)}
        except TaskLeaseLostError as exc:
            return {"key": lease.key, "state": "discarded", "error": str(exc)}
        except Exception as exc:
            state: TaskState = (
                "superseded"
                if isinstance(exc, TaskInputsChangedError)
                else "blocked"
                if isinstance(exc, (TaskBlockedError, TaskDeadlineError, RequestBudgetError, ResponseLimitError))
                else "retry_wait"
            )
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, TaskDeadlineError):
                with locked(self.db):
                    self.store.expire(now=self.clock())
                    expired = self.store.get(lease.key)
                    if expired and expired["fence"] == lease.fence and expired["state"] != "running":
                        self.db.conn.execute("UPDATE pipeline_tasks SET error=? WHERE key=?", (error, lease.key))
                        self.db.conn.execute(
                            "UPDATE task_attempts SET error=? WHERE task_key=? AND fence=?",
                            (error, lease.key, lease.fence),
                        )
                        return {"key": lease.key, "state": expired["state"], "error": error}
            try:
                with locked(self.db):
                    current = self.store.get(lease.key)
                    if state == "retry_wait" and current and current["attempts"] >= lease.spec.limits.max_attempts:
                        state = "failed"
                    self.store.finish(
                        lease,
                        state,
                        now=self.clock(),
                        error=error,
                        retry_after_s=30,
                        result=exc.result if isinstance(exc, TaskBlockedError) else None,
                    )
            except TaskLeaseLostError:
                state = "superseded"
            return {"key": lease.key, "state": state, "error": error}
        finally:
            if prepared is not None:
                prepared.cleanup()

    def run(self, *, max_tasks: int = 100) -> dict[str, Any]:
        if max_tasks < 1:
            raise ValueError("Worker task budget must be positive")
        completed = []
        for _ in range(max_tasks):
            result = self.run_one()
            if result is None:
                break
            completed.append(result)
        return {"work": completed, **self.store.summary(), "offline": self.offline}


def extraction_input(db: Database, key: str, services: Services, *, ledger_path: Path | None = None) -> str:
    previous = db.filing(key)
    return digest(
        [doc.model_dump(mode="json") for doc in db.documents(key)],
        corrections_fingerprint(db.corrections()),
        services.fingerprint,
        processing_fingerprint(),
        [reading_input(document.id, services) for document in db.documents(key)],
        _model_state(ledger_path or db.path, key),
        {
            name: value
            for name, value in ((previous.strata if previous else {}).items())
            if name.startswith("evaluation_")
        },
    )


def _transcript_input(engine: OcrEngine, document_id: str) -> Any:
    if isinstance(engine, TranscriptOcr):
        path = engine.directory / f"{document_id}.txt"
        if not path.exists():
            return None
        # The task key may inspect a bounded transcript; reading itself retains its
        # existing resource controls. Oversize inputs remain explicit and unsealed.
        if path.stat().st_size > 8 * 1024 * 1024:
            raise TaskBlockedError("Transcript exceeds task input fingerprint byte budget")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if isinstance(engine, ChainOcr):
        return [_transcript_input(item, document_id) for item in engine.engines]
    if isinstance(engine, CachedOcr):
        return _transcript_input(engine.inner, document_id)
    if isinstance(engine, SelectiveOcr):
        return [_transcript_input(engine.primary, document_id), _transcript_input(engine.secondary, document_id)]
    return engine.label


def reading_input(document_id: str, services: Services) -> str:
    return digest(
        services.fingerprint,
        services.reader.version,
        services.reader.policy.identity,
        _transcript_input(services.ocr, document_id),
    )


def linking_context(db: Database) -> str:
    """Non-filing inputs stay fixed while dependency tasks build their filings."""

    def rows_hash(sql: str) -> str:
        value = hashlib.sha256()
        for row in db.conn.execute(sql):
            value.update(canonical(tuple(row)).encode())
        return value.hexdigest()

    return digest(
        rows_hash("SELECT key,data FROM signals ORDER BY key"),
        rows_hash(
            "SELECT d.* FROM link_decisions d JOIN (SELECT a,b,MAX(id) AS id FROM link_decisions GROUP BY a,b) latest ON latest.id=d.id ORDER BY d.a,d.b"
        ),
        db.link_revision(),
        db.project_revision(),
        rows_hash(
            "SELECT f.key,f.data FROM filings f WHERE NOT EXISTS (SELECT 1 FROM document_attachments d WHERE d.filing_key=f.key) ORDER BY f.key"
        ),
    )


def filing_output(db: Database, key: str) -> str:
    row = db.conn.execute("SELECT data FROM filings WHERE key=?", (key,)).fetchone()
    return digest(row[0] if row else None)


def seed_pending_filings(db: Database) -> int:
    """Give document-only groups identities before freezing their evaluation split.

    These are explicitly pending, unclassified records with no machine output or
    extracted values. Blank titles avoid copying unsanitized source names into a
    filing before the ordinary extraction/privacy pass has run.
    """
    added = 0
    with db.transaction():
        for key in db.iter_filing_keys_with_documents():
            if db.filing(key) is not None:
                continue
            documents = db.documents(key)
            first = documents[0]
            parts = key.split(":", 2)
            if len(parts) != 3:
                raise ValueError("Document filing identities require source, region and number")
            db.upsert_filing(
                Filing(
                    key=key,
                    source=first.source,
                    region=parts[1],
                    number=parts[2],
                    title="",
                    document_ids=tuple(document.id for document in documents),
                    versions={"tasks": "pending_extraction"},
                )
            )
            added += 1
    return added


def plan_pipeline(
    db: Database,
    services: Services,
    *,
    generation: str,
    limits: TaskLimits | None = None,
    dependencies: tuple[str, ...] = (),
) -> list[TaskSpec]:
    if not generation:
        raise ValueError("A pipeline generation is required")
    limits = limits or TaskLimits()
    tasks: list[TaskSpec] = []
    reading: dict[str, str] = {}
    extracts = []
    for key in db.iter_filing_keys_with_documents():
        documents = db.documents(key)
        for document in documents:
            if document.id not in reading:
                spec = TaskSpec(
                    "read",
                    document.id,
                    {"document_id": document.id, "policy": reading_input(document.id, services)},
                    dependencies,
                    limits=limits,
                )
                reading[document.id] = spec.key
                tasks.append(spec)
        spec = TaskSpec(
            "extract",
            key,
            {"filing_key": key, "input": extraction_input(db, key, services)},
            tuple(sorted({reading[d.id] for d in documents})),
            limits=limits,
            llm_request_keys=tuple(row[0] for row in _model_state(db.path, key)),
        )
        extracts.append(spec.key)
        tasks.append(spec)
    tasks.append(
        TaskSpec(
            "link",
            "dataset",
            {"generation": generation, "algorithm": processing_fingerprint(), "context": linking_context(db)},
            tuple(extracts) or dependencies,
            limits=limits,
        )
    )
    return tasks


def _model_requests(db_path: Path, key: str) -> list[str]:
    return [str(row[0]) for row in _model_state(db_path, key) if row[1] != "succeeded"]


def _model_state(db_path: Path, key: str) -> list[tuple[Any, ...]]:
    path = db_path.parent / "llm-jobs.sqlite"
    if not path.is_file():
        return []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT j.key,j.state,j.answer,j.parsed_by,j.error FROM jobs j JOIN dependencies d ON d.job_key=j.key WHERE d.filing=? ORDER BY j.key",
                (key,),
            )
        ]


def application_handlers(
    db: Database,
    services: Services,
    *,
    http: PoliteHttp | None = None,
    collect: Callable[[TaskSpec, Database], Any] | None = None,
    input_guard: Callable[[], bool] = lambda: True,
    collector_policy: str = "",
) -> dict[str, Handler]:
    """Connect reading, existing extraction/grouping and optional existing collectors.

    Services must be constructed in cached model mode. Generic workers cannot
    submit, reconcile or repeat paid model requests.
    """

    def read(lease: TaskLease) -> PreparedWork:
        if not input_guard():
            raise TaskInputsChangedError("External steering files changed")
        document_id = str(lease.spec.inputs["document_id"])
        revision = TaskStore(db.conn).revision()
        row = db.conn.execute(
            "SELECT data FROM document_attachments WHERE document_id=? LIMIT 1", (document_id,)
        ).fetchone()
        if row is None or lease.spec.inputs.get("policy") != reading_input(document_id, services):
            raise TaskInputsChangedError("Document or reading policy changed")
        selected = DocumentRecord.model_validate_json(row[0])
        if services.blobs.path_for(document_id).stat().st_size > lease.spec.limits.max_bytes:
            raise TaskBlockedError("Document exceeds immutable task byte budget")
        content = read_documents([selected], services)[0]
        result = {"document_id": document_id, "coverage": content.coverage_summary(), "read_by": content.read_by}
        if content.format != "pdf" and not content.text.strip() and content.format != "zip":
            raise TaskBlockedError(
                "Document has no usable text; manual reading or a supported parser is required", result=result
            )
        if content.format == "zip":
            member = db.conn.execute(
                "SELECT 1 FROM document_attachments WHERE json_extract(data,'$.parent_document_id')=? LIMIT 1",
                (document_id,),
            ).fetchone()
            if member is None:
                raise TaskBlockedError("Archive contains no ingested readable member", result=result)
            blocked_member = db.conn.execute(
                "SELECT state,url FROM dossier_edges WHERE filing_key=? AND root_url=? AND state!='document' LIMIT 1",
                (selected.filing_key, selected.url + "#archive=" + document_id),
            ).fetchone()
            if blocked_member:
                raise TaskBlockedError(
                    "Archive has an unsupported or budget-limited member: " + str(blocked_member[0]), result=result
                )
        if content.unread_scanned_pages or content.table_errors or content.format_errors or content.useful_unread_pages:
            raise TaskBlockedError("Reading is incomplete; inspect coverage and resource-limit reasons", result=result)
        return PreparedWork(
            revision,
            result,
            lambda _: None,
            validate=lambda: input_guard() and reading_input(document_id, services) == lease.spec.inputs.get("policy"),
        )

    def extract(lease: TaskLease) -> PreparedWork:
        if not input_guard():
            raise TaskInputsChangedError("External steering files changed")
        model_state = _model_state(db.path, str(lease.spec.inputs["filing_key"]))

        def operation(stage: Database) -> Any:
            key = str(lease.spec.inputs["filing_key"])
            if extraction_input(stage, key, services, ledger_path=db.path) != lease.spec.inputs.get("input"):
                raise TaskInputsChangedError("Document, correction or extraction policy changed")
            requests = _model_requests(db.path, key)
            if requests:
                raise TaskBlockedError("Durable model requests require reconciliation: " + ",".join(requests))
            # A private computation must not modify the shared partition manifest.
            # Existing frozen strata are retained; enqueue freezes new groups first.
            previous = stage.filing(key)
            frozen = {
                name: value
                for name, value in (previous.strata if previous else {}).items()
                if name.startswith("evaluation_")
            }
            if services.partitions_path and "evaluation_partition" not in frozen:
                raise TaskBlockedError("Freeze pending filing groups with tasks enqueue before extraction")
            result = process_filings(stage, replace(services, partitions_path=None), only={key})
            if result.failed:
                raise TaskBlockedError(canonical(result.failed))
            current = stage.filing(key)
            if current is not None and frozen:
                with stage.transaction():
                    stage.upsert_filing(current.model_copy(update={"strata": current.strata | frozen}))
            return {**asdict(result), "filing_key": key, "output": filing_output(stage, key)}

        prepared = prepare_snapshot(db, operation)
        prepared.validate = lambda: (
            input_guard() and model_state == _model_state(db.path, str(lease.spec.inputs["filing_key"]))
        )
        return prepared

    def link(lease: TaskLease) -> PreparedWork:
        if not input_guard():
            raise TaskInputsChangedError("External steering files changed")
        if lease.spec.inputs.get("algorithm") != processing_fingerprint():
            raise TaskInputsChangedError("Linking algorithm changed; enqueue a new generation")

        def operation(stage: Database) -> Any:
            if lease.spec.inputs.get("context") != linking_context(stage):
                raise TaskInputsChangedError("Signals, decisions or the published graph changed")
            for dependency in lease.spec.dependencies:
                task = TaskStore(stage.conn).get(dependency)
                if task and task["kind"] == "extract":
                    result = task.get("result") or {}
                    if result.get("output") != filing_output(stage, str(result.get("filing_key", ""))):
                        raise TaskInputsChangedError("A dependency's filing output changed")
            result = link_and_build(stage)
            if services.partitions_path:
                # Validate newly visible joins against frozen splits without
                # modifying the live manifest during private computation.
                build_manifest(
                    stage.filings(),
                    previous=load_manifest(services.partitions_path),
                    projects=stage.projects(),
                    links=(
                        Link(a=decision.a, b=decision.b, reasons=(), verdict=decision.verdict)
                        for decision in stage.iter_current_link_decisions()
                    ),
                )
            return result

        prepared = prepare_snapshot(db, operation)
        prepared.validate = input_guard
        return prepared

    def explore(lease: TaskLease) -> PreparedWork:
        if not input_guard():
            raise TaskInputsChangedError("External source catalog or steering files changed")
        if lease.spec.policy != collector_policy:
            raise TaskInputsChangedError("Source catalog or screening policy changed since enqueue")
        if collect is None:
            raise TaskBlockedError("Collector composition was not configured")

        def operation(stage: Database) -> Any:
            before = {tuple(row[:5]): row[5] for row in stage.conn.execute("SELECT * FROM collection_coverage")}
            report = collect(lease.spec, stage)
            changed = []
            for row in stage.conn.execute("SELECT * FROM collection_coverage"):
                if before.get(tuple(row[:5])) != row[5]:
                    import json

                    changed.append(json.loads(row[5]))
            return {"report": report, "coverage": changed}

        prepared = prepare_snapshot(db, operation)
        prepared.validate = input_guard
        report = prepared.result["report"]
        if (isinstance(report, dict) and report.get("errors")) or any(
            scope.get("state") != "complete" for scope in prepared.result["coverage"]
        ):
            prepared.state = "blocked"
            prepared.error = "Collection coverage is partial; inspect persisted scope/resource-limit reasons"
        return prepared

    def download(lease: TaskLease) -> PreparedWork:
        if not input_guard():
            raise TaskInputsChangedError("External steering files changed")
        if http is None:
            raise TaskBlockedError("Download requires configured PoliteHttp")

        def operation(stage: Database) -> Any:
            from radar.domain.enums import SourceKind
            from radar.pipeline.ingest import ingest_pdf

            value = dict(lease.spec.inputs)
            data = http.get_bytes(str(value["url"]), min_interval_s=5)
            if len(data) > lease.spec.limits.max_bytes:
                raise TaskBlockedError("Download exceeds immutable task byte budget")
            with stage.transaction():
                record = ingest_pdf(
                    stage,
                    services.blobs,
                    data=data,
                    source=SourceKind(value["source"]),
                    region=str(value["region"]),
                    number=str(value["number"]),
                    url=str(value["url"]),
                    title=str(value["title"]),
                    filename=str(value["filename"]),
                )
            return {"document_id": record.id, "filing_key": record.filing_key}

        prepared = prepare_snapshot(db, operation)
        prepared.validate = input_guard
        return prepared

    return {"read": read, "extract": extract, "link": link, "explore": explore, "download": download}
