"""Entry point for every downloaded file: store the bytes once, record the document, attach it to a filing."""

import re
from datetime import datetime

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.ids import filing_key
from radar.domain.models import DocumentRecord
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database, utcnow


def guess_role(filename: str, title: str) -> DocumentRole:
    text = f"{filename} {title}".lower()
    if "cerfa" in text or "formulaire" in text:
        return DocumentRole.FORM
    if "decision" in text or "décision" in text:
        return DocumentRole.DECISION
    if re.search(r"\bavis\b", text):
        return DocumentRole.OPINION
    return DocumentRole.OTHER


def ingest_pdf(
    db: Database,
    blobs: BlobStore,
    *,
    data: bytes,
    source: SourceKind,
    region: str,
    number: str,
    url: str,
    title: str,
    filename: str,
    role: DocumentRole | None = None,
    fetched_at: datetime | None = None,
    audit: str | None = None,
) -> DocumentRecord:
    """Idempotent: the same bytes always give the same document id. Caller owns the transaction."""
    digest = blobs.put(data, ".pdf")
    key = filing_key(source, region, number)
    existing = db.document_attachment(digest, key)
    inferred_role = role or guess_role(filename, title)
    # One PDF may be linked both as a form and as a neutral annex. The neutral
    # alias adds provenance at the dossier edge; it must not erase a known role.
    if (
        role is None
        and existing is not None
        and inferred_role is DocumentRole.OTHER
        and existing.role is not DocumentRole.OTHER
    ):
        if existing.url != url:
            return existing
        inferred_role = existing.role
    content = read_pdf(data)
    record = DocumentRecord(
        id=digest,
        source=source,
        url=url,
        title=title,
        role=inferred_role,
        filing_key=key,
        fetched_at=fetched_at or utcnow(),
        filename=filename,
        pages=content.pages,
        scanned_pages=content.scanned_pages,
        audit=audit,
    )
    db.upsert_document(record)
    return record
