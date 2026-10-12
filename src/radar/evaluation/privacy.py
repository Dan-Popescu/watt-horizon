"""Privacy audit: stored texts must not contain person data. Expected result: an empty list."""

from radar.domain.enums import DocumentRole
from radar.domain.privacy import PersonScrubber
from radar.reading.pdf import PdfReader
from radar.store.blobs import BlobStore
from radar.store.db import Database


def audit_privacy(db: Database, blobs: BlobStore, reader: PdfReader | None = None) -> list[str]:
    """Names from form person fields, civility names, e-mails and phones still present in stored texts."""
    leaks: list[str] = []
    reader = reader or PdfReader()
    for filing in db.filings():
        forms = [
            reader.read(d.id, blobs.get(d.id)).fields for d in db.documents(filing.key) if d.role is DocumentRole.FORM
        ]
        audit = PersonScrubber.for_filing(forms, [filing.developer, filing.legal_name])
        texts = {
            "title": filing.title,
            "description": filing.project_description or "",
            "classification": filing.classification.quote,
            **{f"evidence:{name}": ev.quote for name, ev in filing.evidence.items()},
            "vocabulary": " ".join(filing.vocabulary),
        }
        leaks += [f"{filing.key} {where}" for where, text in texts.items() if audit.leaks(text)]
    return leaks
