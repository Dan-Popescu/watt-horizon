"""Durable non-paid work. All methods participate in the caller's transaction."""

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskState = Literal["pending", "running", "retry_wait", "blocked", "failed", "complete", "superseded"]
TERMINAL = {"blocked", "failed", "superseded"}

# These tables contain the inputs or outputs of existing application operations.
# Task bookkeeping deliberately does not invalidate a worker's data snapshot.
WATCHED_TABLES = (
    "documents",
    "document_attachments",
    "filings",
    "signals",
    "links",
    "projects",
    "corrections",
    "link_decisions",
    "listing_items",
    "machine_changes",
    "project_changes",
    "pipeline_state",
    "processing_outcomes",
    "dataset_state",
    "collection_coverage",
    "dossier_scans",
    "dossier_edges",
    "discovery_queries",
    "discovery_leads",
    "discovery_query_leads",
    "discovery_lead_origins",
    "discovery_attempts",
    "runs",
)

TASK_MIGRATION = """
CREATE TABLE pipeline_tasks (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, slot TEXT NOT NULL, spec TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN
        ('pending','running','retry_wait','blocked','failed','complete','superseded')),
    attempts INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
    lease_until REAL, next_attempt REAL NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0, elapsed_s REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT, result TEXT
);
CREATE INDEX pipeline_tasks_ready ON pipeline_tasks(state,next_attempt,key);
CREATE INDEX pipeline_tasks_slot ON pipeline_tasks(slot,state);
CREATE TABLE task_dependencies (
    task_key TEXT NOT NULL REFERENCES pipeline_tasks(key),
    dependency_key TEXT NOT NULL REFERENCES pipeline_tasks(key),
    PRIMARY KEY(task_key,dependency_key)
);
CREATE TABLE task_attempts (
    task_key TEXT NOT NULL REFERENCES pipeline_tasks(key), fence INTEGER NOT NULL,
    worker TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
    state TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0, error TEXT,
    PRIMARY KEY(task_key,fence)
);
CREATE TABLE task_source_blocks (source TEXT PRIMARY KEY, reason TEXT NOT NULL, blocked_at REAL NOT NULL);
CREATE TABLE task_input_revision (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
INSERT INTO task_input_revision VALUES (1,0);
""" + "".join(
    f"CREATE TRIGGER task_revision_{table}_{event.lower()} AFTER {event} ON {table} "
    "BEGIN UPDATE task_input_revision SET revision=revision+1 WHERE id=1; END;"
    for table in WATCHED_TABLES
    for event in ("INSERT", "UPDATE", "DELETE")
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class TaskLimits:
    max_attempts: int = 3
    max_requests: int = 100
    lease_s: float = 300
    max_runtime_s: float = 900
    max_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_requests < 0 or self.max_bytes < 1:
            raise ValueError("Task attempts/bytes must be positive and requests nonnegative")
        if any(not math.isfinite(v) or v <= 0 for v in (self.lease_s, self.max_runtime_s)):
            raise ValueError("Task lease and runtime must be finite and positive")


@dataclass(frozen=True)
class TaskSpec:
    kind: str
    identity: str
    inputs: dict[str, Any]
    dependencies: tuple[str, ...] = ()
    source: str = ""
    network: bool = False
    limits: TaskLimits = field(default_factory=TaskLimits)
    llm_request_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind or not self.identity:
            raise ValueError("Tasks require a kind and stable identity")
        if self.kind in {"explore", "download"} and (not self.network or not self.source):
            raise ValueError("Exploration/download tasks require network admission and a source identity")

    @property
    def encoded(self) -> str:
        return canonical(asdict(self))

    @property
    def key(self) -> str:
        return hashlib.sha256(self.encoded.encode()).hexdigest()

    @property
    def slot(self) -> str:
        return canonical((self.kind, self.identity))

    @classmethod
    def decode(cls, value: str) -> "TaskSpec":
        data = json.loads(value)
        data["limits"] = TaskLimits(**data["limits"])
        data["dependencies"] = tuple(data["dependencies"])
        data["llm_request_keys"] = tuple(data["llm_request_keys"])
        return cls(**data)


@dataclass(frozen=True)
class TaskLease:
    key: str
    fence: int
    until: float
    spec: TaskSpec


class TaskLeaseLostError(RuntimeError):
    """An expired, replaced or superseded worker cannot publish."""


class TaskStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    def revision(self) -> int:
        return int(self.conn.execute("SELECT revision FROM task_input_revision WHERE id=1").fetchone()[0])

    def enqueue(self, spec: TaskSpec, *, now: float) -> bool:
        encoded = spec.encoded  # freeze nested inputs before any write
        if self.conn.execute("SELECT 1 FROM pipeline_tasks WHERE key=?", (spec.key,)).fetchone():
            return False
        for dependency in spec.dependencies:
            if (
                dependency == spec.key
                or not self.conn.execute("SELECT 1 FROM pipeline_tasks WHERE key=?", (dependency,)).fetchone()
            ):
                raise ValueError(f"Task dependency must already exist: {dependency}")
        self.conn.execute(
            "UPDATE pipeline_tasks SET state='superseded',fence=fence+1,lease_until=NULL,updated_at=?,"
            "error='immutable_inputs_replaced' WHERE slot=? AND state!='superseded' AND key!=?",
            (now, spec.slot, spec.key),
        )
        self.conn.execute(
            "INSERT INTO pipeline_tasks(key,kind,slot,spec,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (spec.key, spec.kind, spec.slot, encoded, now, now),
        )
        self.conn.executemany("INSERT INTO task_dependencies VALUES (?,?)", ((spec.key, d) for d in spec.dependencies))
        return True

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM pipeline_tasks WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["spec"] = json.loads(value["spec"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def expire(self, *, now: float) -> None:
        for row in self.conn.execute(
            "SELECT key,spec,fence,lease_until FROM pipeline_tasks WHERE state='running' AND lease_until<=?", (now,)
        ).fetchall():
            spec = TaskSpec.decode(row["spec"])
            # A crashed attempt is charged its complete reservation, conservatively.
            self.conn.execute(
                "UPDATE pipeline_tasks SET state=CASE WHEN attempts>=? THEN 'failed' ELSE 'retry_wait' END,"
                "elapsed_s=elapsed_s+?,lease_until=NULL,next_attempt=?,updated_at=?,error='lease_expired' WHERE key=?",
                (spec.limits.max_attempts, min(spec.limits.lease_s, spec.limits.max_runtime_s), now, now, row["key"]),
            )
            self.conn.execute(
                "UPDATE task_attempts SET state='expired',finished_at=?,error='lease_expired' WHERE task_key=? AND fence=?",
                (now, row["key"], row["fence"]),
            )

    def claim(self, worker: str, *, now: float, offline: bool) -> TaskLease | None:
        self.expire(now=now)
        self.conn.execute(
            "UPDATE pipeline_tasks SET state='blocked',error='dependency_blocked',updated_at=? "
            "WHERE state IN ('pending','retry_wait') AND EXISTS (SELECT 1 FROM task_dependencies d "
            "JOIN pipeline_tasks p ON p.key=d.dependency_key WHERE d.task_key=pipeline_tasks.key "
            "AND p.state IN ('blocked','failed','superseded'))",
            (now,),
        )
        rows = self.conn.execute(
            "SELECT * FROM pipeline_tasks t WHERE state IN ('pending','retry_wait') AND next_attempt<=? "
            "AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN pipeline_tasks p ON p.key=d.dependency_key "
            "WHERE d.task_key=t.key AND p.state!='complete') ORDER BY created_at,key",
            (now,),
        )
        for row in rows:
            spec = TaskSpec.decode(row["spec"])
            if offline and spec.network:
                continue  # no reservation and no consumed attempt
            blocked = self.conn.execute(
                "SELECT reason FROM task_source_blocks WHERE source=?", (spec.source,)
            ).fetchone()
            if spec.network and blocked:
                self.conn.execute(
                    "UPDATE pipeline_tasks SET state='blocked',error=?,updated_at=? WHERE key=?",
                    (f"source_blocked:{blocked[0]}", now, row["key"]),
                )
                continue
            if row["attempts"] >= spec.limits.max_attempts or row["elapsed_s"] >= spec.limits.max_runtime_s:
                self.conn.execute(
                    "UPDATE pipeline_tasks SET state='failed',error='task_budget_exhausted',updated_at=? WHERE key=?",
                    (now, row["key"]),
                )
                continue
            fence = int(row["fence"]) + 1
            until = now + min(spec.limits.lease_s, spec.limits.max_runtime_s - row["elapsed_s"])
            self.conn.execute(
                "UPDATE pipeline_tasks SET state='running',attempts=attempts+1,fence=?,lease_until=?,"
                "updated_at=?,error=NULL WHERE key=?",
                (fence, until, now, row["key"]),
            )
            self.conn.execute(
                "INSERT INTO task_attempts VALUES (?,?,?,?,NULL,'running',0,NULL)", (row["key"], fence, worker, now)
            )
            return TaskLease(row["key"], fence, until, spec)
        return None

    def check(self, lease: TaskLease, *, now: float) -> None:
        row = self.conn.execute(
            "SELECT state,fence,lease_until FROM pipeline_tasks WHERE key=?", (lease.key,)
        ).fetchone()
        if row is None or row["state"] != "running" or row["fence"] != lease.fence or row["lease_until"] <= now:
            raise TaskLeaseLostError("Task lease expired or inputs were superseded")
        if (
            lease.spec.source
            and self.conn.execute("SELECT 1 FROM task_source_blocks WHERE source=?", (lease.spec.source,)).fetchone()
        ):
            raise TaskLeaseLostError("Task source was suspended")

    def request(self, lease: TaskLease, *, now: float) -> None:
        self.check(lease, now=now)
        used = self.conn.execute("SELECT requests FROM pipeline_tasks WHERE key=?", (lease.key,)).fetchone()[0]
        if used >= lease.spec.limits.max_requests:
            raise ValueError("Persistent physical HTTP request budget exhausted")
        self.conn.execute("UPDATE pipeline_tasks SET requests=requests+1 WHERE key=?", (lease.key,))
        self.conn.execute(
            "UPDATE task_attempts SET requests=requests+1 WHERE task_key=? AND fence=?", (lease.key, lease.fence)
        )

    def finish(
        self,
        lease: TaskLease,
        state: TaskState,
        *,
        now: float,
        result: Any = None,
        error: str | None = None,
        retry_after_s: float = 0,
    ) -> None:
        self.check(lease, now=now)
        started = self.conn.execute(
            "SELECT started_at FROM task_attempts WHERE task_key=? AND fence=?", (lease.key, lease.fence)
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE pipeline_tasks SET state=?,lease_until=NULL,updated_at=?,next_attempt=?,elapsed_s=elapsed_s+?,"
            "result=?,error=? WHERE key=?",
            (state, now, now + retry_after_s, max(0, now - started), canonical(result), error, lease.key),
        )
        self.conn.execute(
            "UPDATE task_attempts SET state=?,finished_at=?,error=? WHERE task_key=? AND fence=?",
            (state, now, error, lease.key, lease.fence),
        )

    def block_source(self, source: str, reason: str, *, now: float) -> None:
        self.conn.execute(
            "INSERT INTO task_source_blocks VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET reason=excluded.reason,blocked_at=excluded.blocked_at",
            (source, reason, now),
        )
        # Other active workers lose their fence as well as their permission to send.
        self.conn.execute(
            "UPDATE pipeline_tasks SET state='blocked',fence=fence+1,lease_until=NULL,error=?,updated_at=? "
            "WHERE json_extract(spec,'$.source')=? AND json_extract(spec,'$.network')=1 "
            "AND state IN ('pending','retry_wait','running')",
            ("source_blocked:" + reason, now, source),
        )
        self.conn.execute(
            "UPDATE task_attempts SET state='blocked',finished_at=?,error=? WHERE state='running' "
            "AND task_key IN (SELECT key FROM pipeline_tasks WHERE json_extract(spec,'$.source')=?)",
            (now, "source_blocked:" + reason, source),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "states": {
                str(row[0]): int(row[1])
                for row in self.conn.execute("SELECT state,COUNT(*) FROM pipeline_tasks GROUP BY state")
            },
            "physical_requests": int(
                self.conn.execute("SELECT COALESCE(SUM(requests),0) FROM pipeline_tasks").fetchone()[0]
            ),
            "source_blocks": [
                dict(row) for row in self.conn.execute("SELECT * FROM task_source_blocks ORDER BY source")
            ],
        }
