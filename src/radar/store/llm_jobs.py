"""Durable paid work, independent of the disposable result cache.

SQLite reservations coordinate local workers. They do not promise provider-side
exactly-once delivery: an interrupted submission is uncertain until reconciled.
Prices are supplied by the operator; unknown prices never become zero costs.
"""

import json
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BudgetLimits:
    daily_usd: float | None = None
    campaign_usd: float | None = None
    filing_usd: float | None = None
    max_attempts: int = 3
    lease_seconds: float = 300

    def __post_init__(self) -> None:
        amounts = (self.daily_usd, self.campaign_usd, self.filing_usd)
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in amounts):
            raise ValueError("LLM budgets must be finite nonnegative USD amounts")
        if self.max_attempts < 1 or not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("LLM attempts and submission lease must be positive")

    @property
    def priced(self) -> bool:
        return any(v is not None for v in (self.daily_usd, self.campaign_usd, self.filing_usd))


@dataclass(frozen=True)
class TokenPricing:
    version: str
    input_per_million: float
    output_per_million: float
    batch_multiplier: float = 1

    def __post_init__(self) -> None:
        if not self.version or any(
            not math.isfinite(value) or value < 0
            for value in (self.input_per_million, self.output_per_million, self.batch_multiplier)
        ):
            raise ValueError("LLM pricing requires a version and finite nonnegative rates")

    def micros(self, input_tokens: int, output_tokens: int, *, batch: bool) -> int:
        factor = self.batch_multiplier if batch else 1
        return math.ceil((input_tokens * self.input_per_million + output_tokens * self.output_per_million) * factor)


class LlmJobStore:
    def __init__(
        self,
        path: Path | str,
        *,
        limits: BudgetLimits | None = None,
        pricing: Mapping[str, TokenPricing] | None = None,
        campaign: str = "default",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = limits or BudgetLimits()
        self.pricing = dict(pricing or {})
        self.campaign = campaign
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;"
            "CREATE TABLE IF NOT EXISTS jobs ("
            "key TEXT PRIMARY KEY, record TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, submission_id TEXT, raw_message TEXT, answer TEXT, parsed_by TEXT, error TEXT);"
            "CREATE TABLE IF NOT EXISTS dependencies ("
            "job_key TEXT NOT NULL REFERENCES jobs(key), filing TEXT NOT NULL, PRIMARY KEY(job_key, filing));"
            "CREATE TABLE IF NOT EXISTS submissions ("
            "id TEXT PRIMARY KEY, mode TEXT NOT NULL, state TEXT NOT NULL, provider_id TEXT UNIQUE, "
            "created REAL NOT NULL, lease_until REAL NOT NULL, error TEXT);"
            "CREATE TABLE IF NOT EXISTS attempts ("
            "submission_id TEXT NOT NULL REFERENCES submissions(id), job_key TEXT NOT NULL REFERENCES jobs(key), "
            "campaign TEXT NOT NULL, day TEXT NOT NULL, reserved_micros INTEGER, actual_micros INTEGER, "
            "pricing_version TEXT, pricing_snapshot TEXT, actual_model TEXT, input_tokens INTEGER, output_tokens INTEGER, raw_response TEXT, "
            "PRIMARY KEY(submission_id, job_key));"
            "CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);"
            "CREATE INDEX IF NOT EXISTS jobs_state_key ON jobs(state,key);"
            "CREATE INDEX IF NOT EXISTS jobs_submission ON jobs(submission_id,key);"
            "CREATE INDEX IF NOT EXISTS submissions_state_mode_id ON submissions(state,mode,id);"
            "CREATE INDEX IF NOT EXISTS dependencies_filing ON dependencies(filing);"
            "CREATE INDEX IF NOT EXISTS attempts_day ON attempts(day);"
            "CREATE INDEX IF NOT EXISTS attempts_campaign ON attempts(campaign);"
            "CREATE TABLE IF NOT EXISTS legacy_imports (marker TEXT PRIMARY KEY);"
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(attempts)")}
        if "pricing_snapshot" not in columns:
            self.conn.execute("ALTER TABLE attempts ADD COLUMN pricing_snapshot TEXT")
        if "parsed_by" not in {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN parsed_by TEXT")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def add(self, key: str, record: dict[str, Any], filing: str, answer: Any = None) -> None:
        with self._transaction():
            self.conn.execute("INSERT OR IGNORE INTO jobs(key, record) VALUES (?, ?)", (key, json.dumps(record)))
            self.conn.execute("INSERT OR IGNORE INTO dependencies VALUES (?, ?)", (key, filing))
            if answer is not None:
                self.conn.execute(
                    "UPDATE jobs SET answer=?, state='succeeded', error=NULL WHERE key=? AND answer IS NULL",
                    (json.dumps(answer), key),
                )

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ("record", "raw_message", "answer"):
            result[name] = json.loads(result[name]) if result[name] is not None else None
        return result

    def filings(self, key: str) -> list[str]:
        return [
            r[0] for r in self.conn.execute("SELECT filing FROM dependencies WHERE job_key=? ORDER BY filing", (key,))
        ]

    def waiting(self, filing: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM dependencies d JOIN jobs j ON j.key=d.job_key "
                "WHERE d.filing=? AND j.state != 'succeeded' LIMIT 1",
                (filing,),
            ).fetchone()
            is not None
        )

    def entries(self, *states: str) -> list[tuple[str, dict[str, Any]]]:
        return list(self.iter_entries(*states))

    def has_pending(self) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE state='pending' LIMIT 1").fetchone() is not None

    def count_states(self, *states: str) -> int:
        if not states:
            return 0
        marks = ",".join("?" for _ in states)
        return int(self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN ({marks})", states).fetchone()[0])

    def iter_entries(self, *states: str, page_size: int = 100) -> Iterator[tuple[str, dict[str, Any]]]:
        """Read bounded keyset pages, releasing each cursor before callers update jobs."""
        if page_size < 1:
            raise ValueError("Job page size must be positive")
        if not states:
            return
        marks = ",".join("?" for _ in states)
        after: str | None = None
        while True:
            cursor = self.conn.execute(
                f"SELECT key,record FROM jobs WHERE state IN ({marks}) AND (? IS NULL OR key>?) ORDER BY key LIMIT ?",
                (*states, after, after, page_size),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                return
            after = rows[-1][0]
            for row in rows:
                yield row[0], json.loads(row[1])

    def _spent(self, where: str, args: tuple[Any, ...]) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_micros,reserved_micros,0)),0) FROM attempts WHERE " + where, args
        ).fetchone()
        return int(row[0])

    def _reservation(self, key: str, record: dict[str, Any], mode: str, day: str) -> tuple[int | None, str | None]:
        params = record["params"]
        price = self.pricing.get(str(params.get("model", "")))
        if price is None:
            return None, "missing_model_price" if self.limits.priced else None
        # UTF-8 bytes plus overhead deliberately overestimate ordinary token counts.
        # This is an operator-price reservation, not a guarantee of a provider invoice cap.
        input_bound = len(json.dumps(params, ensure_ascii=False).encode()) + 1024
        amount = price.micros(input_bound, int(params.get("max_tokens", 0)), batch=mode == "batch")
        scopes: list[tuple[float | None, int]] = []
        for limit, where, args in (
            (self.limits.daily_usd, "day=?", (day,)),
            (self.limits.campaign_usd, "campaign=?", (self.campaign,)),
            *(
                (self.limits.filing_usd, "job_key IN (SELECT job_key FROM dependencies WHERE filing=?)", (f,))
                for f in self.filings(key)
            ),
        ):
            if limit is None:
                continue
            if self.conn.execute(
                "SELECT 1 FROM attempts WHERE actual_micros IS NULL "
                "AND (reserved_micros IS NULL OR raw_response IS NOT NULL) AND " + where + " LIMIT 1",
                args,
            ).fetchone():
                return amount, "unsettled_prior_cost"
            scopes.append((limit, self._spent(where, args)))
        if any(limit is not None and spent + amount > math.floor(limit * 1_000_000) for limit, spent in scopes):
            return amount, "budget_exhausted"
        return amount, None

    def expire_claims(self) -> None:
        with self._transaction():
            self.conn.execute(
                "UPDATE submissions SET state='uncertain',error='submission_lease_expired' "
                "WHERE state='submitting' AND lease_until<?",
                (time.time(),),
            )
            self.conn.execute(
                "UPDATE jobs SET state='uncertain', error='submission_lease_expired' "
                "WHERE state='submitting' AND submission_id IN (SELECT id FROM submissions WHERE state='uncertain')"
            )

    def claim(
        self, *, mode: str, limit: int, key: str | None = None, max_bytes: int | None = None
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        self.expire_claims()
        now = time.time()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        submission = uuid.uuid4().hex
        claimed: list[tuple[str, dict[str, Any]]] = []
        payload_size = 2
        with self._transaction():
            # Re-evaluate budget blocks on the next invocation (a new day/config can unblock them).
            candidates = self.conn.execute(
                "SELECT key FROM jobs WHERE state IN ('pending','budget_blocked') "
                "AND (? IS NULL OR key=?) ORDER BY state='pending' DESC,key LIMIT ?",
                (key, key, limit),
            ).fetchall()
            self.conn.execute(
                "INSERT INTO submissions(id,mode,state,created,lease_until) VALUES (?,?,?,?,?)",
                (submission, mode, "submitting", now, now + self.limits.lease_seconds),
            )
            for candidate in candidates:
                row = self.conn.execute("SELECT key,record,attempts FROM jobs WHERE key=?", (candidate[0],)).fetchone()
                job_key, record = row[0], json.loads(row[1])
                request_size = (
                    len(json.dumps({"custom_id": job_key, "params": record["params"]}, ensure_ascii=False).encode()) + 2
                )
                if max_bytes is not None and request_size + 2 > max_bytes:
                    self.conn.execute(
                        "UPDATE jobs SET state='blocked',error='request_payload_too_large' WHERE key=?", (job_key,)
                    )
                    continue
                if max_bytes is not None and payload_size + request_size > max_bytes:
                    break
                if row[2] >= self.limits.max_attempts:
                    self.conn.execute("UPDATE jobs SET state='blocked',error='max_attempts' WHERE key=?", (job_key,))
                    continue
                amount, error = self._reservation(job_key, record, mode, day)
                if error:
                    self.conn.execute("UPDATE jobs SET state='budget_blocked',error=? WHERE key=?", (error, job_key))
                    continue
                price = self.pricing.get(str(record["params"].get("model", "")))
                self.conn.execute(
                    "INSERT INTO attempts(submission_id,job_key,campaign,day,reserved_micros,pricing_version,pricing_snapshot) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        submission,
                        job_key,
                        self.campaign,
                        day,
                        amount,
                        price.version if price else None,
                        json.dumps({model: asdict(p) for model, p in self.pricing.items()}),
                    ),
                )
                self.conn.execute(
                    "UPDATE jobs SET state='submitting',submission_id=?,attempts=attempts+1,error=NULL WHERE key=?",
                    (submission, job_key),
                )
                claimed.append((job_key, record))
                payload_size += request_size
            if not claimed:
                self.conn.execute("DELETE FROM submissions WHERE id=?", (submission,))
        return submission, claimed

    def submitted(self, submission: str, provider_id: str) -> None:
        with self._transaction():
            self.conn.execute(
                "UPDATE submissions SET state='in_flight',provider_id=?,error=NULL WHERE id=?",
                (provider_id, submission),
            )
            self.conn.execute("UPDATE jobs SET state='in_flight',error=NULL WHERE submission_id=?", (submission,))

    def uncertain(self, submission: str, error: str) -> None:
        with self._transaction():
            self.conn.execute("UPDATE submissions SET state='uncertain',error=? WHERE id=?", (error, submission))
            self.conn.execute("UPDATE jobs SET state='uncertain',error=? WHERE submission_id=?", (error, submission))

    def resolve_uncertain(
        self, submission: str, *, provider_batch_id: str | None = None, confirm_not_accepted: bool = False
    ) -> None:
        if bool(provider_batch_id) == confirm_not_accepted:
            raise ValueError("Supply a provider batch id OR confirmation that no request was accepted")
        with self._transaction():
            row = self.conn.execute("SELECT state,mode FROM submissions WHERE id=?", (submission,)).fetchone()
            if row is None or row[0] != "uncertain":
                raise ValueError("Only uncertain submissions can be reconciled")
            if provider_batch_id:
                if row[1] != "batch":
                    raise ValueError("A direct-call ambiguity cannot be reconciled with a batch id")
                self.conn.execute(
                    "UPDATE submissions SET state='in_flight',provider_id=?,error=NULL WHERE id=?",
                    (provider_batch_id, submission),
                )
                self.conn.execute("UPDATE jobs SET state='in_flight',error=NULL WHERE submission_id=?", (submission,))
            else:
                self.conn.execute("UPDATE submissions SET state='not_accepted',error=NULL WHERE id=?", (submission,))
                self.conn.execute(
                    "UPDATE attempts SET reserved_micros=0,actual_micros=0 WHERE submission_id=?", (submission,)
                )
                self.conn.execute("UPDATE jobs SET state='pending',error=NULL WHERE submission_id=?", (submission,))

    def in_flight(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.iter_in_flight())

    def iter_in_flight_ids(self, *, page_size: int = 100) -> Iterator[str]:
        if page_size < 1:
            raise ValueError("Batch page size must be positive")
        after: str | None = None
        while True:
            cursor = self.conn.execute(
                "SELECT id,provider_id FROM submissions WHERE state='in_flight' AND mode='batch' "
                "AND (? IS NULL OR id>?) ORDER BY id LIMIT ?",
                (after, after, page_size),
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                return
            after = rows[-1][0]
            for row in rows:
                yield str(row[1])

    def iter_in_flight(self, *, page_size: int = 100) -> Iterator[tuple[str, dict[str, Any]]]:
        """Materialise membership for one batch, never all outstanding request payloads."""
        for provider_id in self.iter_in_flight_ids(page_size=page_size):
            cursor = self.conn.execute(
                "SELECT key FROM jobs WHERE submission_id=(SELECT id FROM submissions WHERE provider_id=?)",
                (provider_id,),
            )
            try:
                entries: dict[str, dict[str, Any]] = {row[0]: {} for row in cursor}
            finally:
                cursor.close()
            yield provider_id, {"entries": entries}

    def received(self, key: str, message: dict[str, Any]) -> None:
        with self._transaction():
            row = self.conn.execute("SELECT record,submission_id FROM jobs WHERE key=?", (key,)).fetchone()
            if row is None:
                return
            record = json.loads(row[0])
            actual_model = str(message.get("model") or record["params"].get("model", ""))
            usage = message.get("usage") or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            price_row = self.conn.execute(
                "SELECT pricing_snapshot FROM attempts WHERE submission_id=? AND job_key=?", (row[1], key)
            ).fetchone()
            snapshot = json.loads(price_row[0]) if price_row and price_row[0] else {}
            price = TokenPricing(**snapshot[actual_model]) if actual_model in snapshot else None
            actual = None
            mode_row = self.conn.execute("SELECT mode FROM submissions WHERE id=?", (row[1],)).fetchone()
            if price is not None and input_tokens is not None and output_tokens is not None:
                total_input = (
                    int(input_tokens)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                )
                actual = price.micros(total_input, int(output_tokens), batch=bool(mode_row and mode_row[0] == "batch"))
            self.conn.execute(
                "UPDATE attempts SET raw_response=?,actual_model=?,input_tokens=?,output_tokens=?,actual_micros=?,"
                "pricing_version=COALESCE(?,pricing_version) WHERE submission_id=? AND job_key=?",
                (
                    json.dumps(message),
                    actual_model,
                    input_tokens,
                    output_tokens,
                    actual,
                    price.version if price else None,
                    row[1],
                    key,
                ),
            )
            self.conn.execute(
                "UPDATE jobs SET raw_message=?,state='received',error=NULL WHERE key=?", (json.dumps(message), key)
            )
            self.conn.execute("UPDATE submissions SET state='done' WHERE id=? AND mode='direct'", (row[1],))

    def succeeded(self, key: str, answer: Any, *, parser_name: str | None = None) -> None:
        with self._transaction():
            self.conn.execute(
                "UPDATE jobs SET state='succeeded',answer=?,parsed_by=?,error=NULL WHERE key=?",
                (json.dumps(answer), parser_name, key),
            )
            self.conn.execute(
                "UPDATE submissions SET state='done' WHERE mode='direct' AND id=(SELECT submission_id FROM jobs WHERE key=?)",
                (key,),
            )

    def parse_failed(self, key: str, error: str, *, missing_parser: bool = False) -> None:
        self.conn.execute(
            "UPDATE jobs SET state=?,error=? WHERE key=?",
            ("await_parser" if missing_parser else "parse_failed", error, key),
        )

    def failed(self, key: str, error: str, raw: Any = None) -> None:
        with self._transaction():
            self.conn.execute(
                "UPDATE jobs SET state=CASE WHEN attempts>=? THEN 'blocked' ELSE 'pending' END,error=? WHERE key=?",
                (self.limits.max_attempts, error, key),
            )
            self.conn.execute(
                "UPDATE attempts SET raw_response=? WHERE submission_id=(SELECT submission_id FROM jobs WHERE key=?) AND job_key=?",
                (json.dumps(raw), key, key),
            )

    def finish_batch(self, provider_id: str) -> None:
        self.conn.execute("UPDATE submissions SET state='done' WHERE provider_id=?", (provider_id,))

    def block(self, key: str, error: str) -> None:
        self.conn.execute("UPDATE jobs SET state='blocked',error=? WHERE key=?", (error, key))

    def settle_cost(self, submission_id: str, key: str, *, actual_usd: float) -> None:
        """Record an externally confirmed charge, including a confirmed unbilled failure.

        This is an explicit reconciliation action, never inferred from a timeout
        or missing usage. The raw provider response remains intact.
        """
        if not math.isfinite(actual_usd) or actual_usd < 0:
            raise ValueError("A settled charge must be a finite nonnegative USD amount")
        changed = self.conn.execute(
            "UPDATE attempts SET actual_micros=?,pricing_version='externally-confirmed' "
            "WHERE submission_id=? AND job_key=?",
            (math.ceil(actual_usd * 1_000_000), submission_id, key),
        ).rowcount
        if not changed:
            raise ValueError("Unknown submission/request charge")

    def summary(self) -> dict[str, Any]:
        self.expire_claims()
        states = dict(self.conn.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"))
        totals = self.conn.execute(
            "SELECT COUNT(*),SUM(CASE WHEN actual_micros IS NULL THEN reserved_micros ELSE 0 END),"
            "SUM(actual_micros),SUM(input_tokens),SUM(output_tokens),"
            "SUM(CASE WHEN actual_micros IS NULL THEN 1 ELSE 0 END) FROM attempts"
        ).fetchone()
        uncertain = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id,mode,error FROM submissions WHERE state='uncertain' ORDER BY created,id LIMIT 100"
            )
        ]
        uncertain_total = int(
            self.conn.execute("SELECT COUNT(*) FROM submissions WHERE state='uncertain'").fetchone()[0]
        )
        issues = [
            {"key": row[0], "state": row[1], "error": row[2]}
            for row in self.conn.execute(
                "SELECT key,state,error FROM jobs WHERE error IS NOT NULL ORDER BY key LIMIT 100"
            )
        ]
        return {
            "states": states,
            "attempts": totals[0],
            "reserved_usd": totals[1] / 1_000_000 if totals[1] is not None else None,
            "accounted_usd": totals[2] / 1_000_000 if totals[2] is not None else None,
            "input_tokens": totals[3] or 0,
            "output_tokens": totals[4] or 0,
            "unpriced_or_unsettled_attempts": totals[5] or 0,
            "uncertain_submissions": uncertain,
            "uncertain_submissions_total": uncertain_total,
            "uncertain_submissions_truncated": uncertain_total > len(uncertain),
            "issues": issues,
            "budget_basis": "operator-supplied prices; conservative reservations, not provider invoice guarantees",
        }

    def import_legacy(self, source: str, pending: list[tuple[str, Any]], batches: list[tuple[str, Any]]) -> None:
        for batch_id, batch in batches:
            marker = f"{source}:batch:{batch_id}"
            if self.conn.execute("SELECT 1 FROM legacy_imports WHERE marker=?", (marker,)).fetchone():
                continue
            # Import before marking; interruption repeats idempotent inserts without deleting the source.
            for key, record in batch["entries"].items():
                self.add(key, record, record["filing"])
            with self._transaction():
                submission = f"legacy:{batch_id}"
                self.conn.execute(
                    "INSERT OR IGNORE INTO submissions(id,mode,state,provider_id,created,lease_until) VALUES (?,?,?,?,?,?)",
                    (submission, "batch", "in_flight", batch_id, time.time(), 0),
                )
                for key in batch["entries"]:
                    self.conn.execute(
                        "UPDATE jobs SET state='in_flight',submission_id=?,attempts=MAX(attempts,1) WHERE key=? AND state='pending'",
                        (submission, key),
                    )
                    self.conn.execute(
                        "INSERT OR IGNORE INTO attempts(submission_id,job_key,campaign,day,pricing_version) VALUES (?,?,?,?,?)",
                        (submission, key, "legacy", datetime.now(UTC).date().isoformat(), "legacy-unpriced"),
                    )
                self.conn.execute("INSERT OR IGNORE INTO legacy_imports VALUES (?)", (marker,))
        for key, record in pending:
            self.add(key, record, record["filing"])

    def close(self) -> None:
        self.conn.close()
