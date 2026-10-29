"""Durable query/lead provenance; callers own transactions on the main database connection."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

DISCOVERY_MIGRATION = """
CREATE TABLE IF NOT EXISTS discovery_queries (
    key TEXT PRIMARY KEY, region TEXT NOT NULL, query TEXT NOT NULL, provider TEXT NOT NULL,
    domains TEXT NOT NULL, seed_keys TEXT NOT NULL, reason TEXT NOT NULL,
    cursor TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'pending',
    requests INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
    results INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS discovery_queries_state ON discovery_queries(state, region, key);
CREATE TABLE IF NOT EXISTS discovery_leads (
    id TEXT PRIMARY KEY, region TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    snippet TEXT NOT NULL, origin TEXT NOT NULL, source_url TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', filing_key TEXT, documents INTEGER NOT NULL DEFAULT 0,
    error TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(region,url)
);
CREATE INDEX IF NOT EXISTS discovery_leads_state ON discovery_leads(state,region,id);
CREATE TABLE IF NOT EXISTS discovery_query_leads (
    query_key TEXT NOT NULL REFERENCES discovery_queries(key),
    lead_id TEXT NOT NULL REFERENCES discovery_leads(id), rank INTEGER NOT NULL,
    observed_url TEXT NOT NULL, PRIMARY KEY(query_key,lead_id)
);
CREATE TABLE IF NOT EXISTS discovery_lead_origins (
    lead_id TEXT NOT NULL REFERENCES discovery_leads(id), origin TEXT NOT NULL,
    source_url TEXT NOT NULL, PRIMARY KEY(lead_id,origin,source_url)
);
CREATE TABLE IF NOT EXISTS discovery_attempts (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, object_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running', started_at TEXT NOT NULL, finished_at TEXT,
    requests INTEGER NOT NULL DEFAULT 0, results INTEGER NOT NULL DEFAULT 0,
    duration_s REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS discovery_attempts_object ON discovery_attempts(kind,object_key,id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class DiscoveryStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection

    def enqueue(
        self,
        *,
        key: str,
        region: str,
        query: str,
        provider: str,
        domains: tuple[str, ...],
        seed_keys: tuple[str, ...],
        reason: str,
    ) -> bool:
        timestamp = now()
        previous = self.query(key)
        if previous:
            merged = sorted(set(previous["seed_keys"]) | set(seed_keys))
            self.conn.execute("UPDATE discovery_queries SET seed_keys=? WHERE key=?", (json.dumps(merged), key))
            return False
        self.conn.execute(
            "INSERT INTO discovery_queries (key,region,query,provider,domains,seed_keys,reason,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (key, region, query, provider, json.dumps(domains), json.dumps(seed_keys), reason, timestamp, timestamp),
        )
        return True

    @staticmethod
    def _query_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for name in ("domains", "seed_keys", "cursor"):
            value[name] = json.loads(value[name])
        return value

    def query(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM discovery_queries WHERE key=?", (key,)).fetchone()
        return self._query_row(row) if row is not None else None

    def queries(self, region: str, *, limit: int, retry_failed: bool = False) -> list[dict[str, Any]]:
        states = ("pending", "partial", "failed") if retry_failed else ("pending", "partial")
        slots = ",".join("?" for _ in states)
        return [
            self._query_row(row)
            for row in self.conn.execute(
                f"SELECT * FROM discovery_queries WHERE region=? AND state IN ({slots}) ORDER BY created_at,key LIMIT ?",
                (region, *states, limit),
            )
        ]

    def query_state(
        self, key: str, state: str, *, cursor: dict[str, str] | None = None, error: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE discovery_queries SET state=?,cursor=COALESCE(?,cursor),error=?,updated_at=? WHERE key=?",
            (state, json.dumps(cursor) if cursor is not None else None, error, now(), key),
        )

    def start_attempt(self, kind: str, key: str) -> int:
        row = self.conn.execute(
            "INSERT INTO discovery_attempts(kind,object_key,started_at) VALUES (?,?,?)", (kind, key, now())
        )
        if row.lastrowid is None:
            raise RuntimeError("Discovery attempt was not persisted")
        if kind != "query":
            self.conn.execute("UPDATE discovery_leads SET attempts=attempts+1,updated_at=? WHERE id=?", (now(), key))
        return row.lastrowid

    def record_request(self, attempt: int, query_key: str | None = None) -> None:
        self.conn.execute("UPDATE discovery_attempts SET requests=requests+1 WHERE id=?", (attempt,))
        if query_key:
            self.conn.execute(
                "UPDATE discovery_queries SET requests=requests+1,updated_at=? WHERE key=?", (now(), query_key)
            )

    def recover_interrupted(self, region: str) -> None:
        """Call only while holding the application's writer lock, after any former worker exited."""
        self.conn.execute(
            "UPDATE discovery_queries SET state='partial',error='interrupted_attempt',updated_at=? "
            "WHERE region=? AND state='running'",
            (now(), region),
        )
        self.conn.execute(
            "UPDATE discovery_leads SET state='partial',error='interrupted_attempt',updated_at=? "
            "WHERE region=? AND state='running'",
            (now(), region),
        )
        self.conn.execute(
            "UPDATE discovery_attempts SET state='interrupted',finished_at=?,error='previous_worker_interrupted' "
            "WHERE state='running' AND object_key IN (SELECT key FROM discovery_queries WHERE region=? "
            "UNION SELECT id FROM discovery_leads WHERE region=?)",
            (now(), region, region),
        )

    def finish_attempt(
        self,
        attempt: int,
        state: str,
        *,
        requests: int = 1,
        results: int = 0,
        duration_s: float = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE discovery_attempts SET state=?,finished_at=?,requests=?,results=?,duration_s=?,error=? WHERE id=?",
            (state, now(), requests, results, duration_s, error, attempt),
        )

    def page_done(self, key: str, results: int) -> None:
        self.conn.execute(
            "UPDATE discovery_queries SET pages=pages+1,results=results+?,updated_at=? WHERE key=?",
            (results, now(), key),
        )

    def add_lead(
        self,
        *,
        region: str,
        url: str,
        title: str,
        snippet: str,
        origin: str,
        source_url: str,
        query_key: str | None = None,
        rank: int = 0,
        observed_url: str | None = None,
    ) -> str:
        identity = hashlib.sha256(json.dumps([region, url]).encode()).hexdigest()
        timestamp = now()
        self.conn.execute(
            "INSERT INTO discovery_leads(id,region,url,title,snippet,origin,source_url,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
            "snippet=excluded.snippet,updated_at=excluded.updated_at",
            (identity, region, url, title[:500], snippet[:1200], origin, source_url, timestamp, timestamp),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO discovery_lead_origins(lead_id,origin,source_url) VALUES (?,?,?)",
            (identity, origin, source_url),
        )
        if query_key:
            self.conn.execute(
                "INSERT OR IGNORE INTO discovery_query_leads(query_key,lead_id,rank,observed_url) VALUES (?,?,?,?)",
                (query_key, identity, rank, observed_url or url),
            )
        return identity

    def leads(self, region: str, *, limit: int, retry_failed: bool = False) -> list[dict[str, Any]]:
        states = ("pending", "failed", "partial", "unconfirmed", "budget") if retry_failed else ("pending", "partial")
        slots = ",".join("?" for _ in states)
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT * FROM discovery_leads WHERE region=? AND state IN ({slots}) ORDER BY attempts,created_at,id LIMIT ?",
                (region, *states, limit),
            )
        ]

    def press_leads(self, region: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 0 < limit <= 1000:
            raise ValueError("Press lead planning limit must be between one and one thousand")
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM discovery_leads WHERE region=? AND origin='press' AND state IN ('pending','unconfirmed') "
                "ORDER BY created_at,id LIMIT ?",
                (region, limit),
            )
        ]

    def lead_state(
        self,
        identity: str,
        state: str,
        *,
        error: str | None = None,
        filing_key: str | None = None,
        documents: int | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE discovery_leads SET state=?,error=?,filing_key=COALESCE(?,filing_key),documents=COALESCE(?,documents),updated_at=? WHERE id=?",
            (state, error, filing_key, documents, now(), identity),
        )

    def status(self, region: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        if not 0 < limit <= 1000:
            raise ValueError("Discovery diagnostic limit must be between one and one thousand")
        where, args = (" WHERE region=?", (region,)) if region else ("", ())
        queries = dict(
            self.conn.execute("SELECT state,COUNT(*) FROM discovery_queries" + where + " GROUP BY state", args)
        )
        leads = dict(self.conn.execute("SELECT state,COUNT(*) FROM discovery_leads" + where + " GROUP BY state", args))
        totals = self.conn.execute(
            "SELECT COALESCE(SUM(requests),0),COALESCE(SUM(pages),0),COALESCE(SUM(results),0) FROM discovery_queries"
            + where,
            args,
        ).fetchone()
        rows = self.conn.execute(
            "SELECT id,region,url,state,error,filing_key,documents FROM discovery_leads"
            + where
            + " ORDER BY updated_at DESC LIMIT ?",
            (*args, limit),
        )
        providers = [
            row[0] for row in self.conn.execute("SELECT DISTINCT provider FROM discovery_queries" + where, args)
        ]
        fees = (
            0 if providers and all(provider == "https://html.duckduckgo.com/html/" for provider in providers) else None
        )
        return {
            "queries": queries,
            "leads": leads,
            "provider_requests": totals[0],
            "provider_pages": totals[1],
            "provider_results": totals[2],
            "recent_leads": [dict(row) for row in rows],
            "model_calls": 0,
            "provider_cost_usd": fees,
            "cost_scope": "provider_access_only; cloud_compute_not_measured; no_paid_model_calls",
            "caveat": "Pistes non confirmées distinctes des faits ; disponibilité du moteur à qualifier en conditions réelles.",
        }
