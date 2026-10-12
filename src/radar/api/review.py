"""Human review (internal): list filings with machine vs current values, record corrections and validations.

Writes go through `submit_review`; the caller then rebuilds the filing (`process_filings(only=...)` + links)
so corrections are applied by the same code path as every nightly run.
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from radar.api.views import CheckView, CorrectionView, DocumentView, EvidenceView, ReviewItem
from radar.classification.gold import GoldLabel
from radar.domain.enums import Category
from radar.domain.fields import field_values, machine_values
from radar.domain.models import Correction, Filing
from radar.domain.privacy import PersonScrubber
from radar.pipeline.corrections import VALIDATED, validate_value
from radar.store.db import Database

_NOTES = PersonScrubber()


def _priority(item: ReviewItem) -> tuple[int, str]:
    """Never reviewed and doubtful first, then never reviewed, then reviewed."""
    if item.reviewed_at is None:
        return (0 if item.confidence == "review" else 1, item.number)
    return (2, item.number)


def review_items(db: Database, labels: dict[str, GoldLabel]) -> list[ReviewItem]:
    corrections: dict[str, list[Correction]] = {}
    for c in db.corrections():
        corrections.setdefault(c.filing_key, []).append(c)
    items = [_item(db, f, labels.get(f.number), corrections.get(f.key, [])) for f in db.filings()]
    return sorted(items, key=_priority)


def _item(db: Database, f: Filing, label: GoldLabel | None, corrections: Iterable[Correction]) -> ReviewItem:
    docs = {d.id: d for d in db.documents(f.key)}
    machine = f.machine.classification if f.machine else f.classification
    return ReviewItem(
        key=f.key,
        number=f.number,
        title=f.title,
        category=f.classification.category.value,
        machine_category=machine.category.value,
        machine_method=machine.method,
        draft_label=label.category.value if label and not label.verified else None,
        confidence=f.confidence.value,
        reviewed_at=f.reviewed_at.isoformat() if f.reviewed_at else None,
        values=field_values(f),
        machine_values=machine_values(f),
        evidence={
            name: EvidenceView(
                field=name,
                quote=ev.quote,
                locator=ev.locator,
                document_url=docs[ev.document_id].url if ev.document_id in docs else None,
            )
            for name, ev in f.evidence.items()
        },
        documents=[
            DocumentView(title=d.title, url=d.url, role=d.role.value, read_by=f.reading.get(d.id, ""))
            for d in docs.values()
        ],
        checks=[CheckView(name=c.name, status=c.status.value, detail=c.detail) for c in f.checks],
        corrections=[
            CorrectionView(
                id=c.id or 0,
                field=c.field,
                value=c.value,
                note=c.note,
                author=c.author,
                created_at=c.created_at.isoformat(timespec="seconds"),
            )
            for c in corrections
        ],
    )


def submit_review(
    db: Database,
    filing_key: str,
    *,
    author: str,
    changes: dict[str, str | float | None],
    note: str = "",
    validate: bool = False,
    now: datetime | None = None,
) -> list[int]:
    """Record field corrections (and optionally "everything else is right"). Raises ValueError on bad input."""
    filing = db.filing(filing_key)
    if filing is None:
        raise ValueError(f"unknown filing {filing_key}")
    undecided = filing.classification.category in {Category.UNCLASSIFIED, Category.NEEDS_REVIEW}
    if validate and undecided and "category" not in changes:
        raise ValueError("choisir une catégorie avant de valider ce dossier")
    author = author.strip()
    if not author:
        raise ValueError("author is required")
    if not changes and not validate:
        raise ValueError("nothing to record")
    at = now or datetime.now(UTC)
    clean_note = _NOTES.scrub(note.strip()) or ""
    rows = [
        Correction(
            filing_key=filing_key,
            field=name,
            value=validate_value(name, value),
            note=clean_note,
            author=author,
            created_at=at,
        )
        for name, value in changes.items()
    ]
    if validate:
        rows.append(
            Correction(
                filing_key=filing_key, field=VALIDATED, value=None, note=clean_note, author=author, created_at=at
            )
        )
    with db.transaction():
        return [db.add_correction(c) for c in rows]
