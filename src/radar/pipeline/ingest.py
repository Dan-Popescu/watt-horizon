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
) -> DocumentRecord:
    """Idempotent: the same bytes always give the same document id. Caller owns the transaction."""
    digest = blobs.put(data, ".pdf")
    content = read_pdf(data)
    record = DocumentRecord(
        id=digest,
        source=source,
        url=url,
        title=title,
        role=role or guess_role(filename, title),
        filing_key=filing_key(source, region, number),
        fetched_at=fetched_at or utcnow(),
        filename=filename,
        pages=content.pages,
        scanned_pages=content.scanned_pages,
    )
    db.upsert_document(record)
    return record
