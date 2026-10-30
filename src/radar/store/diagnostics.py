"""Revision-bound aggregate snapshots, computed by jobs and only read by APIs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from radar.store.db import Database

DEFINITION_VERSION = 1

_SOURCE_TABLES = (
    "documents",
    "document_attachments",
    "filings",
    "signals",
    "links",
    "projects",
    "runs",
    "project_changes",
    "corrections",
    "machine_changes",
    "link_decisions",
    "listing_items",
    "processing_outcomes",
    "collection_coverage",
    "dossier_scans",
    "dossier_edges",
    "campaign_receipts",
    "campaign_applied_fields",
    "discovery_queries",
    "discovery_leads",
    "discovery_query_leads",
    "discovery_lead_origins",
    "discovery_attempts",
)

DIAGNOSTICS_MIGRATION = (
    "CREATE TABLE diagnostic_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);"
    "INSERT INTO diagnostic_revision VALUES(1,0);"
    + "".join(
        f"CREATE TRIGGER diagnostic_{table}_{operation.lower()} AFTER {operation} ON {table} "
        "BEGIN UPDATE diagnostic_revision SET revision=revision+1 WHERE id=1; END;"
        for table in _SOURCE_TABLES
        for operation in ("INSERT", "UPDATE", "DELETE")
    )
)


def revision(db: Database) -> int:
    return int(db.conn.execute("SELECT revision FROM diagnostic_revision WHERE id=1").fetchone()[0])


def external_token(*paths: Path) -> str:
    """Version external local inputs, including uncheckpointed model journal changes."""
    digest = hashlib.sha256()
    for path in paths:
        if path.is_dir():
            # The case reader uses only direct JSON children. Ignore unrelated
            # assets rather than hashing an entire private directory per request.
            members = sorted(member for member in path.glob("*.json") if member.is_file())
            for member in members:
                digest.update(str(member.relative_to(path)).encode())
                digest.update(member.read_bytes())
        else:
            for member in (path, Path(str(path) + "-wal")):
                try:
                    stat = member.stat()
                    digest.update(json.dumps([str(member), stat.st_size, stat.st_mtime_ns]).encode())
                except FileNotFoundError:
                    digest.update(json.dumps([str(member), None]).encode())
    return digest.hexdigest()


def refresh(
    db: Database,
    name: str,
    build: Callable[[], dict[str, Any]],
    *,
    scope: dict[str, Any],
    external: Callable[[], str] = lambda: "",
) -> dict[str, Any]:
    """Compute under one read snapshot; publish only if its exact database inputs still match."""
    if db.conn.in_transaction:
        raise ValueError("Diagnostics refresh requires its own read snapshot")
    before_external = external()
    db.conn.execute("BEGIN")
    try:
        source_revision = revision(db)
        value = build()
    finally:
        db.conn.rollback()
    if external() != before_external:
        raise ValueError("External diagnostic inputs changed while computing the snapshot")
    envelope = {
        "definition_version": DEFINITION_VERSION,
        "computed_at": datetime.now(UTC).isoformat(),
        "source_revision": source_revision,
        "external_token": before_external,
        "scope": scope,
        "data": value,
    }
    with db.transaction():
        if revision(db) != source_revision:
            raise ValueError("Dataset changed while computing the diagnostic snapshot")
        db.set_pipeline_state("diagnostics:" + name, envelope)
    return value | {"snapshot": {key: val for key, val in envelope.items() if key != "data"} | {"status": "current"}}


def read(
    db: Database,
    name: str,
    *,
    scope: dict[str, Any],
    external: str = "",
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Never recompute in a read request or silently serve an old snapshot as current."""
    stored = db.get_pipeline_state("diagnostics:" + name)
    unavailable = {
        "snapshot": {"status": "unavailable", "computed_at": None, "source_revision": None, "scope": scope},
        "quality_measured": False,
    }
    if not stored or not isinstance(stored.get("data"), dict):
        return unavailable
    try:
        at = datetime.fromisoformat(stored["computed_at"])
        if at.tzinfo is None:
            return unavailable
        current_revision = revision(db)
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return unavailable
    reasons = []
    if stored.get("definition_version") != DEFINITION_VERSION:
        reasons.append("definition_changed")
    if stored.get("source_revision") != current_revision:
        reasons.append("dataset_changed")
    if stored.get("external_token") != external:
        reasons.append("external_inputs_changed")
    if stored.get("scope") != scope:
        reasons.append("scope_changed")
    if (now or datetime.now(UTC)) - at > max_age:
        reasons.append("expired")
    metadata = {key: value for key, value in stored.items() if key != "data"}
    metadata.update(status="stale" if reasons else "current", stale_reasons=reasons, current_revision=current_revision)
    data: dict[str, Any] = stored["data"]
    return data | {"snapshot": metadata}
