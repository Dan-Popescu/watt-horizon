"""A content-addressed PDF can belong to several filings without losing provenance."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.models import DocumentRecord
from radar.store.db import _MIGRATIONS, Database


def document(filing_key: str, title: str) -> DocumentRecord:
    return DocumentRecord(
        id="same-physical-pdf",
        source=SourceKind.MANUAL,
        url="https://example.test/pdf",
        title=title,
        role=DocumentRole.FORM,
        filing_key=filing_key,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        filename="synthetic.pdf",
    )


def test_shared_pdf_keeps_both_filing_attachments(db: Database) -> None:
    first, second = document("case:1", "First context"), document("case:2", "Second context")
    with db.transaction():
        db.upsert_document(first)
        db.upsert_document(second)
    assert db.documents("case:1") == [first]
    assert db.documents("case:2") == [second]
    assert db.filing_keys_with_documents() == ["case:1", "case:2"]
    assert db.counts()["documents"] == 1
    assert db.documents() == [first, second]
    with db.transaction():
        db.upsert_document(second.model_copy(update={"title": "Updated second context"}))
    assert db.documents("case:1") == [first]
    assert db.documents("case:2")[0].title == "Updated second context"


def test_migration_retains_existing_document_attachment(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    original = document("case:legacy", "Legacy context")
    with sqlite3.connect(path) as conn:
        for migration in _MIGRATIONS[:7]:
            conn.executescript(migration)
        conn.execute("PRAGMA user_version = 7")
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            (original.id, original.filing_key, original.source.value, original.url, original.model_dump_json()),
        )
    with Database(path) as migrated:
        assert migrated.documents("case:legacy") == [original]
        with migrated.transaction():
            migrated.upsert_document(document("case:new", "New context"))
        assert migrated.documents("case:legacy") == [original]
