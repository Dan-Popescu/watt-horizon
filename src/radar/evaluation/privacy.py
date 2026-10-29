"""Privacy audit: stored texts must not contain person data. Expected result: an empty list."""

from radar.domain.enums import DocumentRole
from radar.domain.privacy import PersonScrubber
from radar.reading.pdf import PdfReader
from radar.store.blobs import BlobStore
from radar.store.db import Database


class PrivacyAuditIncompleteError(RuntimeError):
    """A source could not be checked; absence of a completed audit must never count as zero leaks."""


def audit_privacy(db: Database, blobs: BlobStore, reader: PdfReader | None = None) -> list[str]:
    """Names from form person fields, civility names, e-mails and phones still present in stored texts."""
    leaks: list[str] = []
    reader = reader or PdfReader()
    for filing in db.filings():
        forms = []
        for document in db.documents(filing.key):
            if document.role is not DocumentRole.FORM:
                continue
            try:
                data = blobs.get(document.id, max_bytes=reader.max_bytes)
                forms.append(reader.read(document.id, data).fields)
            except (ValueError, OSError, RuntimeError) as exc:
                raise PrivacyAuditIncompleteError(
                    f"privacy audit incomplete: document {document.id} "
                    f"could not be checked ({type(exc).__name__}; source unreadable or outside resource limits)"
                ) from exc
        audit = PersonScrubber.for_filing(forms, [filing.developer, filing.legal_name])
        texts = {
            "title": filing.title,
            "description": filing.project_description or "",
            "classification": filing.classification.quote,
            **{f"evidence:{name}": ev.quote for name, ev in filing.evidence.items()},
            "vocabulary": " ".join(filing.vocabulary),
            **{
                f"quantity:{index}:{name}": str(value or "")
                for index, observation in enumerate(filing.quantity_observations)
                for name, value in (
                    ("quote", observation.quote),
                    ("header", observation.header_quote),
                    ("phase", observation.phase),
                    ("scenario", observation.scenario),
                )
            },
        }
        leaks += [f"{filing.key} {where}" for where, text in texts.items() if audit.leaks(text)]
    return leaks
