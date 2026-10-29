"""SQLite repository. Full objects are stored as validated JSON; a few columns are indexed for queries.

Schema changes: bump `SCHEMA_VERSION` and add a step to `_MIGRATIONS` (never edit a past step).
"""

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack, cast

from radar.domain.geo import bbox_around, haversine_km
from radar.domain.models import Correction, DocumentRecord, Filing, Link, LinkDecision, Project, ProjectChange, Signal
from radar.domain.text import fold
from radar.store.discovery import DISCOVERY_MIGRATION
from radar.store.review_history import REVIEW_MIGRATION

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
    """
    CREATE TABLE link_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT NOT NULL, b TEXT NOT NULL, verdict TEXT NOT NULL,
        author TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX link_decisions_pair ON link_decisions(a, b);
    """,
    """
    CREATE TABLE document_attachments (
        document_id TEXT NOT NULL REFERENCES documents(id), filing_key TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (document_id, filing_key)
    );
    CREATE INDEX document_attachments_filing ON document_attachments(filing_key);
    INSERT INTO document_attachments (document_id, filing_key, data) SELECT id, filing_key, data FROM documents;
    """,
    """
    ALTER TABLE listing_items ADD COLUMN audit_policy INTEGER NOT NULL DEFAULT 1;
    """,
    """
    CREATE INDEX projects_power_id ON projects(mw DESC, id);
    CREATE INDEX projects_category_power ON projects(category, mw DESC, id);
    CREATE INDEX links_b ON links(b);
    """,
    """
    CREATE TABLE dataset_state (id INTEGER PRIMARY KEY CHECK(id = 1), projects_revision INTEGER NOT NULL);
    INSERT INTO dataset_state VALUES (1, 0);
    """,
    """
    ALTER TABLE dataset_state ADD COLUMN links_revision INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE pipeline_state (name TEXT PRIMARY KEY, data TEXT NOT NULL);
    """,
    """
    CREATE TABLE processing_outcomes (
        key TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('running', 'complete', 'waiting', 'failed')),
        error TEXT,
        input_revision INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX processing_outcomes_state ON processing_outcomes(state, key);
    CREATE TRIGGER document_input_insert AFTER INSERT ON document_attachments BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        VALUES (NEW.filing_key,'waiting','document_inputs_changed',1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='document_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER document_input_update AFTER UPDATE ON document_attachments
    WHEN OLD.data IS NOT NEW.data OR OLD.document_id IS NOT NEW.document_id OR OLD.filing_key IS NOT NEW.filing_key
    BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        SELECT OLD.filing_key,'waiting','document_inputs_changed',1 WHERE OLD.filing_key IS NOT NEW.filing_key
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='document_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        VALUES (NEW.filing_key,'waiting','document_inputs_changed',1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='document_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER document_input_delete AFTER DELETE ON document_attachments BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        VALUES (OLD.filing_key,'waiting','document_inputs_changed',1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='document_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER correction_input_insert AFTER INSERT ON corrections WHEN NEW.active=1 BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        VALUES (NEW.filing_key,'waiting','correction_inputs_changed',1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='correction_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER correction_input_update AFTER UPDATE ON corrections
    WHEN (OLD.active=1 OR NEW.active=1) AND (
        OLD.id IS NOT NEW.id OR OLD.filing_key IS NOT NEW.filing_key OR OLD.field IS NOT NEW.field
        OR OLD.value IS NOT NEW.value OR OLD.note IS NOT NEW.note OR OLD.author IS NOT NEW.author
        OR OLD.created_at IS NOT NEW.created_at OR OLD.cause IS NOT NEW.cause OR OLD.blind IS NOT NEW.blind
        OR OLD.active IS NOT NEW.active)
    BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        SELECT OLD.filing_key,'waiting','correction_inputs_changed',1
        WHERE OLD.active=1 AND (OLD.filing_key IS NOT NEW.filing_key OR NEW.active!=1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='correction_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        SELECT NEW.filing_key,'waiting','correction_inputs_changed',1 WHERE NEW.active=1
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='correction_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER correction_input_delete AFTER DELETE ON corrections WHEN OLD.active=1 BEGIN
        INSERT INTO processing_outcomes(key,state,error,input_revision)
        VALUES (OLD.filing_key,'waiting','correction_inputs_changed',1)
        ON CONFLICT(key) DO UPDATE SET state='waiting',error='correction_inputs_changed',
            input_revision=processing_outcomes.input_revision+1;
    END;
    CREATE TRIGGER linking_filing_insert AFTER INSERT ON filings BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_filing_update AFTER UPDATE ON filings WHEN OLD.data IS NOT NEW.data BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_filing_delete AFTER DELETE ON filings BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_signal_insert AFTER INSERT ON signals BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_signal_update AFTER UPDATE ON signals WHEN OLD.data IS NOT NEW.data BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_signal_delete AFTER DELETE ON signals BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_decision_insert AFTER INSERT ON link_decisions BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_decision_update AFTER UPDATE ON link_decisions
    WHEN OLD.id IS NOT NEW.id OR OLD.a IS NOT NEW.a OR OLD.b IS NOT NEW.b OR OLD.verdict IS NOT NEW.verdict
        OR OLD.author IS NOT NEW.author OR OLD.created_at IS NOT NEW.created_at OR OLD.note IS NOT NEW.note
    BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    CREATE TRIGGER linking_decision_delete AFTER DELETE ON link_decisions BEGIN
        INSERT INTO pipeline_state VALUES ('linking_dirty','true') ON CONFLICT(name)
        DO UPDATE SET data='true' WHERE pipeline_state.data IS NOT 'true';
    END;
    """,
)
_MIGRATIONS += (
    """
    CREATE TABLE collection_coverage (
        source TEXT NOT NULL, region TEXT NOT NULL, period TEXT NOT NULL, scope TEXT NOT NULL,
        mode TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(source, region, period, scope, mode)
    );
    """,
)
_MIGRATIONS += (
    """
    CREATE TABLE dossier_scans (
        filing_key TEXT NOT NULL, root_url TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(filing_key, root_url)
    );
    CREATE TABLE dossier_edges (
        filing_key TEXT NOT NULL, root_url TEXT NOT NULL, parent_url TEXT NOT NULL,
        url TEXT NOT NULL, state TEXT NOT NULL, document_id TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(filing_key, root_url, parent_url, url)
    );
    CREATE INDEX dossier_edges_filing ON dossier_edges(filing_key);
    """,
    """
    CREATE INDEX dossier_scans_root ON dossier_scans(root_url, filing_key);
    CREATE INDEX dossier_edges_url ON dossier_edges(filing_key, root_url, url);
    """,
)
_MIGRATIONS += (REVIEW_MIGRATION, DISCOVERY_MIGRATION)
SCHEMA_VERSION = len(_MIGRATIONS)


class ProjectFilters(TypedDict, total=False):
    categories: Iterable[str] | None
    department: str | None
    near: tuple[float, float, float] | None
    min_mw: float | None
    substation: str | None
    since: str | None
    commune: str | None
    query: str | None
    bounds: tuple[float, float, float, float] | None


class ProjectQuery(ProjectFilters, total=False):
    limit: int | None
    after: tuple[float | None, str] | None


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = path = Path(path)
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        # Review edits and the nightly job may write at the same time: wait for the lock instead of failing.
        self.conn = (
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            if read_only
            else sqlite3.connect(path, timeout=30)
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("radar_lower", 1, lambda value: str(value or "").lower(), deterministic=True)
        self.conn.create_function(
            "radar_distance",
            4,
            lambda a, b, c, d: None if c is None or d is None else haversine_km(a, b, c, d),
            deterministic=True,
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        if read_only:
            if self.conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                self.conn.close()
                raise ValueError("Database schema requires migration before serving reads")
            self.conn.execute("BEGIN")
        else:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; ~5 ms less per commit
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
                # executescript otherwise commits before executing its statements. Keep the schema
                # and its version in the same explicit transaction, including on startup failure.
                self.conn.executescript(
                    "BEGIN IMMEDIATE;\n" + _MIGRATIONS[version] + f"\nPRAGMA user_version = {version + 1};\nCOMMIT;"
                )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn:
            yield

    # Documents -------------------------------------------------------------------------------------
    def upsert_document(self, doc: DocumentRecord) -> None:
        self.conn.execute(
            "INSERT INTO documents (id, filing_key, source, url, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (doc.id, doc.filing_key, doc.source.value, doc.url, doc.model_dump_json()),
        )
        existing = self.conn.execute(
            "SELECT json_extract(data, '$.fetched_at') FROM document_attachments WHERE document_id=? AND filing_key=?",
            (doc.id, doc.filing_key),
        ).fetchone()
        if existing is not None:
            first = datetime.fromisoformat(existing[0])
            # Legacy timestamps without an offset use the store's UTC convention.
            previous = first if first.tzinfo is not None else first.replace(tzinfo=UTC)
            observed = doc.fetched_at if doc.fetched_at.tzinfo is not None else doc.fetched_at.replace(tzinfo=UTC)
            if previous <= observed:
                doc = doc.model_copy(update={"fetched_at": first})
        self.conn.execute(
            "INSERT INTO document_attachments (document_id, filing_key, data) VALUES (?, ?, ?) "
            "ON CONFLICT(document_id, filing_key) DO UPDATE SET data=excluded.data",
            (doc.id, doc.filing_key, doc.model_dump_json()),
        )

    def documents(self, filing_key: str | None = None) -> list[DocumentRecord]:
        """Document contexts, one per filing attachment; physical file counts remain deduplicated."""
        sql, args = "SELECT data FROM document_attachments", cast(tuple[Any, ...], ())
        if filing_key is not None:
            sql, args = sql + " WHERE filing_key = ?", (filing_key,)
        return [
            DocumentRecord.model_validate_json(r["data"])
            for r in self.conn.execute(sql + " ORDER BY document_id, filing_key", args)
        ]

    def document_attachment(self, document_id: str, filing_key: str) -> DocumentRecord | None:
        row = self.conn.execute(
            "SELECT data FROM document_attachments WHERE document_id=? AND filing_key=?", (document_id, filing_key)
        ).fetchone()
        return DocumentRecord.model_validate_json(row[0]) if row else None

    def filing_keys_with_documents(self) -> list[str]:
        return list(self.iter_filing_keys_with_documents())

    def iter_filing_keys_with_documents(self) -> Iterator[str]:
        for row in self.conn.execute("SELECT DISTINCT filing_key FROM document_attachments ORDER BY filing_key"):
            yield row[0]

    # Processing evidence survives partial rebuilds and interrupted processes. ----------------------
    def note_processing_outcome(
        self,
        key: str,
        state: Literal["running", "complete", "waiting", "failed"],
        error: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        """The caller commits running before work and completion alongside the filing write."""
        changed = self.conn.execute(
            "INSERT INTO processing_outcomes(key,state,error) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state,error=excluded.error "
            "WHERE (processing_outcomes.state IS NOT excluded.state OR processing_outcomes.error IS NOT excluded.error) "
            "AND (? IS NULL OR processing_outcomes.input_revision=?)",
            (key, state, error, expected_revision, expected_revision),
        ).rowcount
        return expected_revision is None or bool(changed) or self.processing_input_revision(key) == expected_revision

    def processing_input_revision(self, key: str) -> int:
        row = self.conn.execute("SELECT input_revision FROM processing_outcomes WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row else 0

    def processing_outcome(self, key: str) -> str | None:
        row = self.conn.execute("SELECT state FROM processing_outcomes WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def unfinished_processing_keys(self) -> Iterator[str]:
        for row in self.conn.execute("SELECT key FROM processing_outcomes WHERE state!='complete' ORDER BY key"):
            yield str(row[0])

    def prune_processing_outcomes(self) -> None:
        """Only remove work whose filing and document attachments have both disappeared."""
        self.conn.execute(
            "DELETE FROM processing_outcomes WHERE NOT EXISTS (SELECT 1 FROM filings f WHERE f.key=processing_outcomes.key) "
            "AND NOT EXISTS (SELECT 1 FROM document_attachments d WHERE d.filing_key=processing_outcomes.key)"
        )

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

    def signals(self, keys: Iterable[str] | None = None) -> list[Signal]:
        if keys is None:
            rows = self.conn.execute("SELECT data FROM signals ORDER BY key")
            return [Signal.model_validate_json(r["data"]) for r in rows]
        result: list[Signal] = []
        selected = sorted(set(keys))
        for start in range(0, len(selected), 500):
            chunk = selected[start : start + 500]
            rows = self.conn.execute(
                f"SELECT data FROM signals WHERE key IN ({','.join('?' * len(chunk))}) ORDER BY key", chunk
            )
            result.extend(Signal.model_validate_json(r["data"]) for r in rows)
        return result

    def replace_links(self, links: Iterable[Link]) -> None:
        rows = [(lk.a, lk.b, lk.model_dump_json()) for lk in links]
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS next_links (a TEXT, b TEXT, PRIMARY KEY(a, b))")
        self.conn.execute("DELETE FROM next_links")
        self.conn.executemany("INSERT OR IGNORE INTO next_links VALUES (?, ?)", [(a, b) for a, b, _ in rows])
        before = self.conn.total_changes
        self.conn.execute(
            "DELETE FROM links WHERE NOT EXISTS (SELECT 1 FROM next_links n WHERE n.a=links.a AND n.b=links.b)"
        )
        self.conn.executemany(
            "INSERT INTO links (a, b, data) VALUES (?, ?, ?) "
            "ON CONFLICT(a, b) DO UPDATE SET data=excluded.data WHERE links.data <> excluded.data",
            rows,
        )
        if self.conn.total_changes != before:
            self.conn.execute("UPDATE dataset_state SET links_revision=links_revision+1 WHERE id=1")

    def link_revision(self) -> int:
        return int(self.conn.execute("SELECT links_revision FROM dataset_state WHERE id=1").fetchone()[0])

    def get_pipeline_state(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT data FROM pipeline_state WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def set_pipeline_state(self, name: str, state: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO pipeline_state(name,data) VALUES (?,?) "
            "ON CONFLICT(name) DO UPDATE SET data=excluded.data WHERE pipeline_state.data <> excluded.data",
            (name, json.dumps(state, sort_keys=True)),
        )

    def clear_pipeline_state(self, name: str) -> None:
        self.conn.execute("DELETE FROM pipeline_state WHERE name=?", (name,))

    def links(self, key: str | None = None) -> list[Link]:
        sql, args = "SELECT data FROM links", cast(tuple[Any, ...], ())
        if key is not None:
            sql, args = sql + " WHERE a = ? OR b = ?", (key, key)
        return [Link.model_validate_json(r["data"]) for r in self.conn.execute(sql, args)]

    # Projects --------------------------------------------------------------------------------------
    def replace_projects(self, projects: Iterable[Project]) -> None:
        projects = list(projects)
        self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS next_projects (id TEXT PRIMARY KEY)")
        self.conn.execute("DELETE FROM next_projects")
        self.conn.executemany("INSERT INTO next_projects VALUES (?)", [(p.id,) for p in projects])
        before = self.conn.total_changes
        self.conn.execute("DELETE FROM projects WHERE id NOT IN (SELECT id FROM next_projects)")
        self.conn.executemany(
            "INSERT INTO projects (id, category, department, lat, lon, mw, substation_folded, first_seen, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "category=excluded.category, department=excluded.department, lat=excluded.lat, lon=excluded.lon, "
            "mw=excluded.mw, substation_folded=excluded.substation_folded, first_seen=excluded.first_seen, "
            "updated_at=excluded.updated_at, data=excluded.data WHERE projects.data <> excluded.data",
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
        if self.conn.total_changes != before:
            self.conn.execute("UPDATE dataset_state SET projects_revision=projects_revision+1 WHERE id=1")

    def project_revision(self) -> int:
        return int(self.conn.execute("SELECT projects_revision FROM dataset_state WHERE id=1").fetchone()[0])

    def project(self, project_id: str) -> Project | None:
        row = self.conn.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row["data"]) if row else None

    def projects(self, **filters: Unpack[ProjectQuery]) -> list[Project]:
        return list(self.iter_projects(**filters))

    def iter_projects(
        self,
        *,
        categories: Iterable[str] | None = None,
        department: str | None = None,
        near: tuple[float, float, float] | None = None,
        min_mw: float | None = None,
        substation: str | None = None,
        since: str | None = None,
        commune: str | None = None,
        query: str | None = None,
        bounds: tuple[float, float, float, float] | None = None,
        limit: int | None = None,
        after: tuple[float | None, str] | None = None,
    ) -> Iterator[Project]:
        """Exact filtering and keyset ordering happen before bounded JSON deserialization."""
        where, args = self._project_filter(
            categories=categories,
            department=department,
            near=near,
            min_mw=min_mw,
            substation=substation,
            since=since,
            commune=commune,
            query=query,
            bounds=bounds,
        )
        rank = "radar_distance(:center_lat, :center_lon, lat, lon)" if near else "mw"
        if after is not None:
            value, key = after
            args.update(after_value=value, after_id=key)
            if near:
                where.append(f"({rank} > :after_value OR ({rank} = :after_value AND id > :after_id))")
            elif value is None:
                where.append("(mw IS NULL AND id > :after_id)")
            else:
                where.append("(mw < :after_value OR (mw = :after_value AND id > :after_id) OR mw IS NULL)")
        sql = "SELECT data FROM projects" + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {rank} ASC, id ASC" if near else " ORDER BY mw DESC NULLS LAST, id ASC"
        if limit is not None:
            sql += " LIMIT :result_limit"
            args["result_limit"] = max(0, limit)
        with closing(self.conn.execute(sql, args)) as rows:
            for row in rows:
                yield Project.model_validate_json(row["data"])

    def _project_filter(
        self,
        *,
        categories: Iterable[str] | None = None,
        department: str | None = None,
        near: tuple[float, float, float] | None = None,
        min_mw: float | None = None,
        substation: str | None = None,
        since: str | None = None,
        commune: str | None = None,
        query: str | None = None,
        bounds: tuple[float, float, float, float] | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        where: list[str] = []
        args: dict[str, Any] = {}
        if categories is not None:
            cats = tuple(categories)
            where.append(f"category IN ({','.join(':cat' + str(i) for i in range(len(cats)))})")
            args.update({"cat" + str(i): value for i, value in enumerate(cats)})
        if department:
            where.append("department = :department")
            args["department"] = department
        if near:
            lat_min, lon_min, lat_max, lon_max = bbox_around(*near)
            where.append("lat BETWEEN :lat_min AND :lat_max AND lon BETWEEN :lon_min AND :lon_max")
            where.append("radar_distance(:center_lat, :center_lon, lat, lon) <= :radius")
            args.update(
                lat_min=lat_min,
                lat_max=lat_max,
                lon_min=lon_min,
                lon_max=lon_max,
                center_lat=near[0],
                center_lon=near[1],
                radius=near[2],
            )
        if min_mw is not None:
            where.append("mw >= :min_mw")
            args["min_mw"] = min_mw
        if substation:
            where.append("substation_folded LIKE :substation")
            args["substation"] = f"%{fold(substation)}%"
        if since:
            where.append("first_seen >= :since")
            args["since"] = since
        if commune:
            where.append("radar_lower(json_extract(data, '$.location.commune')) = :commune")
            args["commune"] = commune.lower()
        if query:
            fields = ("name", "developer", "location.commune", "substation")
            where.append(
                "("
                + " OR ".join(f"instr(radar_lower(json_extract(data, '$.{field}')), :query) > 0" for field in fields)
                + ")"
            )
            args["query"] = query.strip().lower()
        if bounds:
            where.append("lat BETWEEN :south AND :north AND lon BETWEEN :west AND :east")
            args.update(zip(("west", "south", "east", "north"), bounds, strict=True))
        return where, args

    def project_stats(self, **filters: Any) -> dict[str, Any]:
        where, args = self._project_filter(**filters)
        suffix = " WHERE " + " AND ".join(where) if where else ""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(mw), 0), COALESCE(SUM(mw IS NULL), 0), "
            "COALESCE(SUM(CASE WHEN json_extract(data, '$.power.status') = 'known' THEN mw ELSE 0 END), 0), "
            "COALESCE(SUM(json_extract(data, '$.confidence') = 'review'), 0) FROM projects" + suffix,
            args,
        ).fetchone()
        return {
            "total": row[0],
            "known_power_mw": round(row[1], 1),
            "projects_without_power": row[2],
            "declared_power_mw": round(row[3], 1),
            "projects_to_review": row[4],
            "by_category": dict(
                self.conn.execute("SELECT category, COUNT(*) FROM projects" + suffix + " GROUP BY category", args)
            ),
        }

    def project_departments(self) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT department FROM projects WHERE department IS NOT NULL ORDER BY department"
            )
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

    # Human link decisions (append-only; the latest per pair wins) ---------------------------------------
    def add_link_decision(self, d: LinkDecision) -> int:
        a, b = sorted((d.a, d.b))
        cur = self.conn.execute(
            "INSERT INTO link_decisions (a, b, verdict, author, created_at, note) VALUES (?, ?, ?, ?, ?, ?)",
            (a, b, d.verdict, d.author, d.created_at.isoformat(), d.note),
        )
        return int(cur.lastrowid or 0)

    def link_decisions(self) -> list[LinkDecision]:
        """Every decision, oldest first (history and export)."""
        return [
            LinkDecision(
                id=r["id"],
                a=r["a"],
                b=r["b"],
                verdict=r["verdict"],
                author=r["author"],
                created_at=datetime.fromisoformat(r["created_at"]),
                note=r["note"],
            )
            for r in self.conn.execute("SELECT * FROM link_decisions ORDER BY id")
        ]

    def current_link_decisions(self) -> dict[tuple[str, str], LinkDecision]:
        """Latest decision per pair, withdrawn ones (« undecided ») left out."""
        latest = {(d.a, d.b): d for d in self.link_decisions()}
        return {pair: d for pair, d in latest.items() if d.verdict != "undecided"}

    # Listing items: every item a collector saw (kept or not), for unbiased screen-recall estimates ---------
    def save_collection_coverage(self, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO collection_coverage(source,region,period,scope,mode,data) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source,region,period,scope,mode) DO UPDATE SET data=excluded.data",
            (*[data[k] for k in ("source", "region", "period", "scope", "mode")], json.dumps(data)),
        )

    def collection_coverage(self, region: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT data FROM collection_coverage", () if region is None else (region,)
        if region is not None:
            sql += " WHERE region=?"
        return [json.loads(row[0]) for row in self.conn.execute(sql + " ORDER BY source,period,scope,mode", args)]

    def save_dossier_scan(self, key: str, root_url: str, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO dossier_scans VALUES (?,?,?) ON CONFLICT(filing_key,root_url) "
            "DO UPDATE SET data=excluded.data",
            (key, root_url, json.dumps(data, ensure_ascii=False)),
        )

    def dossier_scans(self, key: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT filing_key,root_url,data FROM dossier_scans", () if key is None else (key,)
        if key is not None:
            sql += " WHERE filing_key=?"
        return [{"filing_key": r[0], "root_url": r[1], **json.loads(r[2])} for r in self.conn.execute(sql, args)]

    def dossier_keys_for_root(self, root_url: str) -> list[str]:
        return [
            row[0] for row in self.conn.execute("SELECT filing_key FROM dossier_scans WHERE root_url=?", (root_url,))
        ]

    def dossier_summary(self, region: str, limit: int = 20) -> dict[str, Any]:
        """Aggregate in SQL and decode only a bounded diagnostic page."""
        if limit <= 0:
            raise ValueError("Dossier diagnostic limit must be positive")
        where = "substr(filing_key,instr(filing_key,':')+1,?)=?"
        args = (len(region) + 1, region + ":")
        counts = dict(
            self.conn.execute(
                "SELECT COALESCE(json_extract(data,'$.state'),'unknown'),COUNT(*) FROM dossier_scans WHERE "
                + where
                + " GROUP BY 1",
                args,
            ).fetchall()
        )
        rows = self.conn.execute(
            "SELECT filing_key,root_url,data FROM dossier_scans WHERE "
            + where
            + " AND COALESCE(json_extract(data,'$.state'),'unknown') <> 'complete' ORDER BY root_url LIMIT ?",
            (*args, limit),
        )
        incomplete_n = sum(count for state, count in counts.items() if state != "complete")
        return {
            "by_state": counts,
            "incomplete_n": incomplete_n,
            "incomplete": [{"filing_key": r[0], "root_url": r[1], **json.loads(r[2])} for r in rows],
            "truncated": incomplete_n > limit,
        }

    def record_dossier_edge(
        self, key: str, root_url: str, parent_url: str, url: str, state: str, document_id: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO dossier_edges VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(filing_key,root_url,parent_url,url) DO UPDATE SET "
            "state=excluded.state,document_id=excluded.document_id,updated_at=excluded.updated_at",
            (key, root_url, parent_url, url, state, document_id, utcnow().isoformat()),
        )

    def dossier_edges(self, key: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM dossier_edges WHERE filing_key=?", (key,))]

    def settle_dossier_url(self, key: str, root_url: str, url: str, state: str, document_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dossier_edges SET state=?,document_id=?,updated_at=? WHERE filing_key=? AND root_url=? AND url=?",
            (state, document_id, utcnow().isoformat(), key, root_url, url),
        )

    def record_listing_item(self, url: str, source: str, title: str, kept: bool, audit: str | None) -> None:
        self.conn.execute(
            "INSERT INTO listing_items (url, source, title, kept, audit, first_seen, audit_policy) "
            "VALUES (?, ?, ?, ?, ?, ?, 2) "
            "ON CONFLICT(url) DO UPDATE SET title=excluded.title, kept=excluded.kept, audit=excluded.audit, audit_policy=2",
            (url, source, title, int(kept), audit, utcnow().isoformat()),
        )

    def rejected_titles(self) -> list[tuple[str, str]]:
        """(url, title) of every listed item the title screen rejected (negatives for screen-term suggestions)."""
        return [(r[0], r[1]) for r in self.conn.execute("SELECT url, title FROM listing_items WHERE kept = 0")]

    def listing_item_status(self, url: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT source,kept,audit FROM listing_items WHERE url=?", (url,)).fetchone()
        return dict(row) if row is not None else None

    def listing_counts(self) -> dict[str, int]:
        """seen / kept / rejected / audit_random / audit_near_miss over all sources."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(kept), 0), COALESCE(SUM(audit = 'random'), 0), "
            "COALESCE(SUM(audit LIKE 'near-miss:%'), 0), "
            "COALESCE(SUM(kept = 0 AND audit_policy <> 2), 0) FROM listing_items"
        ).fetchone()
        return {
            "seen": row[0],
            "kept": row[1],
            "rejected": row[0] - row[1],
            "audit_random": row[2],
            "audit_near_miss": row[3],
            "audit_policy_version": 1 if row[4] else 2,
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
