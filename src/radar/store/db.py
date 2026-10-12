"""SQLite repository. Full objects are stored as validated JSON; a few columns are indexed for queries.

Schema changes: bump `SCHEMA_VERSION` and add a step to `_MIGRATIONS` (never edit a past step).
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from radar.domain.geo import bbox_around
from radar.domain.models import Correction, DocumentRecord, Filing, Link, Project, ProjectChange, Signal
from radar.domain.text import fold

_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE documents (
        id TEXT PRIMARY KEY, filing_key TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
        data TEXT NOT NULL
    );
    CREATE INDEX documents_filing ON documents(filing_key);
    CREATE TABLE filings (
        key TEXT PRIMARY KEY, source TEXT NOT NULL, region TEXT NOT NULL, number TEXT NOT NULL,
        category TEXT NOT NULL, confidence TEXT NOT NULL, department TEXT, filed_on TEXT, data TEXT NOT NULL
    );
    CREATE INDEX filings_category ON filings(category);
    CREATE TABLE signals (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, department TEXT, observed_on TEXT,
        data TEXT NOT NULL
    );
    CREATE TABLE links (a TEXT NOT NULL, b TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (a, b));
    CREATE TABLE projects (
        id TEXT PRIMARY KEY, category TEXT NOT NULL, department TEXT, lat REAL, lon REAL, mw REAL,
        substation_folded TEXT, first_seen TEXT, updated_at TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE INDEX projects_geo ON projects(lat, lon);
    CREATE INDEX projects_department ON projects(department);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, stats TEXT NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE project_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, project_id TEXT NOT NULL, kind TEXT NOT NULL,
        category TEXT NOT NULL, department TEXT, data TEXT NOT NULL
    );
    CREATE INDEX project_changes_at ON project_changes(at);
    """,
    """
    CREATE TABLE corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, filing_key TEXT NOT NULL, field TEXT NOT NULL, value TEXT,
        note TEXT NOT NULL DEFAULT '', author TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX corrections_filing ON corrections(filing_key);
    """,
    """
    CREATE TABLE listing_items (
        url TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, kept INTEGER NOT NULL,
        audit TEXT, first_seen TEXT NOT NULL
    );
    CREATE INDEX listing_items_source ON listing_items(source);
    """,
    """
    ALTER TABLE corrections ADD COLUMN cause TEXT;
    ALTER TABLE corrections ADD COLUMN blind INTEGER NOT NULL DEFAULT 0;
    """,
    """
    CREATE TABLE machine_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, filing_key TEXT NOT NULL, field TEXT NOT NULL,
        before TEXT, after TEXT, version TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX machine_changes_filing ON machine_changes(filing_key);
    """,
)
SCHEMA_VERSION = len(_MIGRATIONS)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Review edits and the nightly job may write at the same time: wait for the lock instead of failing.
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current, SCHEMA_VERSION):
            with self.conn:
                self.conn.executescript(_MIGRATIONS[version])
                self.conn.execute(f"PRAGMA user_version = {version + 1}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn:
            yield

    # Documents -------------------------------------------------------------------------------------
    def upsert_document(self, doc: DocumentRecord) -> None:
        self.conn.execute(
            "INSERT INTO documents (id, filing_key, source, url, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET filing_key=excluded.filing_key, url=excluded.url, data=excluded.data",
            (doc.id, doc.filing_key, doc.source.value, doc.url, doc.model_dump_json()),
        )

    def documents(self, filing_key: str | None = None) -> list[DocumentRecord]:
        sql, args = "SELECT data FROM documents", cast(tuple[Any, ...], ())
        if filing_key is not None:
            sql, args = sql + " WHERE filing_key = ?", (filing_key,)
        return [DocumentRecord.model_validate_json(r["data"]) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def filing_keys_with_documents(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT filing_key FROM documents ORDER BY filing_key")]

    # Filings ---------------------------------------------------------------------------------------
    def upsert_filing(self, f: Filing) -> None:
        self.conn.execute(
            "INSERT INTO filings (key, source, region, number, category, confidence, department, filed_on, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET category=excluded.category, "
            "confidence=excluded.confidence, department=excluded.department, filed_on=excluded.filed_on, data=excluded.data",
            (
                f.key,
                f.source.value,
                f.region,
                f.number,
                f.classification.category.value,
                f.confidence.value,
                f.location.department,
                f.filed_on.isoformat() if f.filed_on else None,
                f.model_dump_json(),
            ),
        )

    def filings(self, categories: Iterable[str] | None = None) -> list[Filing]:
        sql, args = "SELECT data FROM filings", cast(tuple[Any, ...], ())
        if categories is not None:
            cats = tuple(categories)
            sql, args = sql + f" WHERE category IN ({','.join('?' * len(cats))})", cats
        return [Filing.model_validate_json(r["data"]) for r in self.conn.execute(sql + " ORDER BY key", args)]

    def filing(self, key: str) -> Filing | None:
        row = self.conn.execute("SELECT data FROM filings WHERE key = ?", (key,)).fetchone()
        return Filing.model_validate_json(row["data"]) if row else None

    # Signals and links -----------------------------------------------------------------------------
    def upsert_signal(self, s: Signal) -> None:
        self.conn.execute(
            "INSERT INTO signals (key, kind, source, department, observed_on, data) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET department=excluded.department, observed_on=excluded.observed_on, data=excluded.data",
            (
                s.key,
                s.kind.value,
                s.source.value,
                s.department,
                s.observed_on.isoformat() if s.observed_on else None,
                s.model_dump_json(),
            ),
        )

    def signals(self) -> list[Signal]:
        return [
            Signal.model_validate_json(r["data"]) for r in self.conn.execute("SELECT data FROM signals ORDER BY key")
        ]

    def replace_links(self, links: Iterable[Link]) -> None:
        self.conn.execute("DELETE FROM links")
        self.conn.executemany(
            "INSERT OR REPLACE INTO links (a, b, data) VALUES (?, ?, ?)",
            [(lk.a, lk.b, lk.model_dump_json()) for lk in links],
        )

    def links(self, key: str | None = None) -> list[Link]:
        sql, args = "SELECT data FROM links", cast(tuple[Any, ...], ())
        if key is not None:
            sql, args = sql + " WHERE a = ? OR b = ?", (key, key)
        return [Link.model_validate_json(r["data"]) for r in self.conn.execute(sql, args)]

    # Projects --------------------------------------------------------------------------------------
    def replace_projects(self, projects: Iterable[Project]) -> None:
        self.conn.execute("DELETE FROM projects")
        self.conn.executemany(
            "INSERT INTO projects (id, category, department, lat, lon, mw, substation_folded, first_seen, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    p.id,
                    p.category.value,
                    p.location.department,
                    p.location.lat,
                    p.location.lon,
                    p.power.mw,
                    fold(p.substation) or None,
                    p.first_seen.isoformat() if p.first_seen else None,
                    p.updated_at.isoformat(),
                    p.model_dump_json(),
                )
                for p in projects
            ],
        )

    def project(self, project_id: str) -> Project | None:
        row = self.conn.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row["data"]) if row else None

    def projects(
        self,
        *,
        categories: Iterable[str] | None = None,
        department: str | None = None,
        near: tuple[float, float, float] | None = None,
        min_mw: float | None = None,
        substation: str | None = None,
        since: str | None = None,
    ) -> list[Project]:
        """Coarse SQL filtering; exact distance filtering is left to the caller (see api.queries)."""
        where: list[str] = []
        args: list[Any] = []
        if categories is not None:
            cats = tuple(categories)
            where.append(f"category IN ({','.join('?' * len(cats))})")
            args += cats
        if department:
            where.append("department = ?")
            args.append(department)
        if near:
            lat_min, lon_min, lat_max, lon_max = bbox_around(*near)
            where.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
            args += [lat_min, lat_max, lon_min, lon_max]
        if min_mw is not None:
            where.append("mw >= ?")
            args.append(min_mw)
        if substation:
            where.append("substation_folded LIKE ?")
            args.append(f"%{fold(substation)}%")
        if since:
            where.append("first_seen >= ?")
            args.append(since)
        sql = "SELECT data FROM projects" + (" WHERE " + " AND ".join(where) if where else "")
        return [
            Project.model_validate_json(r["data"])
            for r in self.conn.execute(sql + " ORDER BY mw DESC NULLS LAST", args)
        ]

    # Human corrections (append-only; withdrawing one sets active = 0) ---------------------------------
    def add_correction(self, c: Correction) -> int:
        cur = self.conn.execute(
            "INSERT INTO corrections (filing_key, field, value, note, author, created_at, cause, blind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.filing_key,
                c.field,
                json.dumps(c.value),
                c.note,
                c.author,
                c.created_at.isoformat(),
                c.cause,
                int(c.blind),
            ),
        )
        return int(cur.lastrowid or 0)

    def withdraw_correction(self, correction_id: int) -> bool:
        cur = self.conn.execute("UPDATE corrections SET active = 0 WHERE id = ? AND active = 1", (correction_id,))
        return cur.rowcount > 0

    def corrections(self, filing_key: str | None = None) -> list[Correction]:
        sql, args = "SELECT * FROM corrections WHERE active = 1", cast(tuple[Any, ...], ())
        if filing_key is not None:
            sql, args = sql + " AND filing_key = ?", (filing_key,)
        return [
            Correction(
                id=r["id"],
                filing_key=r["filing_key"],
                field=r["field"],
                value=json.loads(r["value"]) if r["value"] is not None else None,
                note=r["note"],
                author=r["author"],
                created_at=datetime.fromisoformat(r["created_at"]),
                cause=r["cause"],
                blind=bool(r["blind"]),
            )
            for r in self.conn.execute(sql + " ORDER BY id", args)
        ]

    # Listing items: every item a collector saw (kept or not), for unbiased screen-recall estimates ---------
    def record_listing_item(self, url: str, source: str, title: str, kept: bool, audit: str | None) -> None:
        self.conn.execute(
            "INSERT INTO listing_items (url, source, title, kept, audit, first_seen) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET title=excluded.title, kept=excluded.kept, audit=excluded.audit",
            (url, source, title, int(kept), audit, utcnow().isoformat()),
        )

    def listing_counts(self) -> dict[str, int]:
        """seen / kept / rejected / audit_random / audit_near_miss over all sources."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(kept), 0), COALESCE(SUM(audit = 'random'), 0), "
            "COALESCE(SUM(audit LIKE 'near-miss:%'), 0) FROM listing_items"
        ).fetchone()
        return {
            "seen": row[0],
            "kept": row[1],
            "rejected": row[0] - row[1],
            "audit_random": row[2],
            "audit_near_miss": row[3],
        }

    # Machine value changes between builds (code or learning changed an output) ----------------------------
    def add_machine_changes(self, filing_key: str, changes: Iterable[tuple[str, Any, Any]], version: str) -> None:
        at = utcnow().isoformat()
        self.conn.executemany(
            "INSERT INTO machine_changes (at, filing_key, field, before, after, version) VALUES (?, ?, ?, ?, ?, ?)",
            [(at, filing_key, name, json.dumps(a), json.dumps(b), version) for name, a, b in changes],
        )

    def machine_changes(self) -> dict[str, list[dict[str, Any]]]:
        """filing key -> its recorded changes, oldest first."""
        out: dict[str, list[dict[str, Any]]] = {}
        for r in self.conn.execute("SELECT * FROM machine_changes ORDER BY id"):
            out.setdefault(r["filing_key"], []).append(
                {
                    "at": r["at"],
                    "field": r["field"],
                    "before": json.loads(r["before"]),
                    "after": json.loads(r["after"]),
                    "version": r["version"],
                }
            )
        return out

    # Change log -------------------------------------------------------------------------------------
    def add_changes(self, changes: Iterable[ProjectChange]) -> None:
        self.conn.executemany(
            "INSERT INTO project_changes (at, project_id, kind, category, department, data) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (c.at.isoformat(), c.project_id, c.kind.value, c.category.value, c.department, c.model_dump_json())
                for c in changes
            ],
        )

    def changes(
        self,
        *,
        since: str,
        categories: Iterable[str] | None = None,
        department: str | None = None,
        limit: int = 200,
    ) -> list[ProjectChange]:
        """Newest first. `since` is an ISO date or datetime (inclusive)."""
        where: list[str] = ["at >= ?"]
        args: list[Any] = [since]
        if categories is not None:
            cats = tuple(categories)
            where.append(f"category IN ({','.join('?' * len(cats))})")
            args += cats
        if department:
            where.append("department = ?")
            args.append(department)
        sql = f"SELECT data FROM project_changes WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
        return [ProjectChange.model_validate_json(r["data"]) for r in self.conn.execute(sql, [*args, limit])]

    # Runs ------------------------------------------------------------------------------------------
    def start_run(self, kind: str) -> int:
        cur = self.conn.execute("INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, utcnow().isoformat()))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?",
            (utcnow().isoformat(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def last_runs(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT kind, finished_at, stats FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE finished_at IS NOT NULL GROUP BY kind)"
        )
        return {r["kind"]: {"finished_at": r["finished_at"], "stats": json.loads(r["stats"])} for r in rows}

    def runs(self, kind_prefix: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Most recent runs first: kind, started_at, finished_at (None if it crashed or is running), stats."""
        rows = self.conn.execute(
            "SELECT kind, started_at, finished_at, stats FROM runs WHERE kind LIKE ? ORDER BY id DESC LIMIT ?",
            (kind_prefix + "%", limit),
        )
        return [
            {
                "kind": r["kind"],
                "started_at": datetime.fromisoformat(r["started_at"]),
                "finished_at": datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None,
                "stats": json.loads(r["stats"]),
            }
            for r in rows
        ]

    def counts(self) -> dict[str, int]:
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("documents", "filings", "signals", "links", "projects", "project_changes", "corrections")
        }
