"""Entry point for every downloaded file: store the bytes once, record the document, attach it to a filing."""

import re
from datetime import datetime
from urllib.parse import quote

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.ids import filing_key
from radar.domain.models import DocumentRecord
from radar.reading.formats import (
    DEFAULT_FORMAT_LIMITS,
    FormatLimits,
    archive_members_bounded,
    detect_format,
    read_document_bounded,
)
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
    """Compatibility entry point: all supported formats share the same durable ingestion path."""
    return ingest_document(
        db,
        blobs,
        data=data,
        source=source,
        region=region,
        number=number,
        url=url,
        title=title,
        filename=filename,
        role=role,
        fetched_at=fetched_at,
        audit=audit,
    )


def ingest_document(
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
    limits: FormatLimits = DEFAULT_FORMAT_LIMITS,
    timeout_s: float = 30,
    parent_document_id: str | None = None,
    member_path: str | None = None,
    _depth: int = 0,
    _budget: list[int] | None = None,
) -> DocumentRecord:
    """Store originals and archive lineage; the caller owns the transaction and publication.

    The historical physical ``.pdf`` blob suffix stays compatible with existing readers. Actual format
    is detected from bytes, never inferred from a potentially misleading URL or extension.
    """
    if len(data) > limits.max_bytes:
        raise ValueError("Document exceeds byte limit")
    kind = detect_format(data)
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
    content = (
        read_pdf(data, max_bytes=limits.max_bytes, document_timeout_s=timeout_s)
        if kind == "pdf"
        else read_document_bounded(data, timeout_s=timeout_s, limits=limits)
    )
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
        format=kind,
        parent_document_id=parent_document_id,
        member_path=member_path,
    )
    db.upsert_document(record)
    if kind == "zip":
        budget = _budget if _budget is not None else [0, 0]
        archive_url = url + "#archive=" + digest
        if _depth >= limits.max_depth:
            db.record_dossier_edge(key, archive_url, archive_url, archive_url + ":depth", "budget")
            raise ValueError("Archive nesting depth limit exceeded")
        for name, child_data in archive_members_bounded(data, timeout_s=timeout_s, limits=limits):
            budget[0] += 1
            budget[1] += len(child_data)
            child_url = archive_url + ":member=" + quote(name, safe="")
            if budget[0] > limits.max_members or budget[1] > limits.max_uncompressed_bytes:
                db.record_dossier_edge(key, archive_url, archive_url, child_url, "budget")
                raise ValueError("Nested archives exceed aggregate member or uncompressed byte limit")
            if detect_format(child_data) == "unsupported":
                # The original archive retains these bytes without executing or pretending to read them.
                db.record_dossier_edge(key, archive_url, archive_url, child_url, "unsupported")
                continue
            child = ingest_document(
                db,
                blobs,
                data=child_data,
                source=source,
                region=region,
                number=number,
                url=child_url,
                title=title,
                filename=name,
                fetched_at=fetched_at,
                audit=audit,
                limits=limits,
                timeout_s=timeout_s,
                parent_document_id=digest,
                member_path=name,
                _depth=_depth + 1,
                _budget=budget,
            )
            db.record_dossier_edge(key, archive_url, archive_url, child_url, "document", child.id)
    return record
