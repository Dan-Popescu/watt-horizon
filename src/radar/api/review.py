"""Human review (internal): list filings with machine vs current values, record corrections and validations.

Writes go through `submit_review`; the caller then rebuilds the filing (`process_filings(only=...)` + links)
so corrections are applied by the same code path as every nightly run.
"""

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime

from radar.api.views import CheckView, CorrectionView, DocumentView, EvidenceView, FlagView, ReviewItem
from radar.classification.gold import GoldLabel
from radar.domain.enums import Category
from radar.domain.fields import field_values, machine_values
from radar.domain.models import Correction, Filing
from radar.domain.privacy import PersonScrubber
from radar.pipeline.corrections import CAUSES, VALIDATED, validate_value
from radar.quality.flags import FLAGS
from radar.quality.triage import triage
from radar.store.db import Database

_NOTES = PersonScrubber()


def review_items(db: Database, labels: dict[str, GoldLabel], today: date | None = None) -> list[ReviewItem]:
    """Every filing, the review queue first (by priority, see quality/triage.py), reviewed ones last."""
    all_corrections = db.corrections()
    corrections: dict[str, list[Correction]] = {}
    for c in all_corrections:
        corrections.setdefault(c.filing_key, []).append(c)
    filings = db.filings()
    queue = {t.key: t for t in triage(filings, all_corrections, today or date.today(), db.machine_changes())}
    items = []
    for f in filings:
        item = _item(db, f, labels.get(f.number), corrections.get(f.key, []))
        if t := queue.get(f.key):
            item = item.model_copy(
                update={"priority": round(t.priority, 2), "reasons": t.reasons, "stale_review": t.stale_review}
            )
        items.append(item)
    return sorted(items, key=lambda i: (-i.priority, i.number))


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
        field_confidence=f.field_confidence,
        auto_accepted=list(f.auto_accepted),
        flags=[
            FlagView(
                code=g.code, severity=g.severity.value, field=g.field, detail=g.detail, label=FLAGS[g.code].label_fr
            )
            for g in f.flags
        ],
        corrections=[
            CorrectionView(
                id=c.id or 0,
                field=c.field,
                value=c.value,
                note=c.note,
                author=c.author,
                created_at=c.created_at.isoformat(timespec="seconds"),
                cause=c.cause,
                blind=c.blind,
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
    cause: str | None = None,
    blind: bool = False,
    now: datetime | None = None,
) -> list[int]:
    """Record field corrections (and optionally "everything else is right"). Raises ValueError on bad input.
    `blind`: values typed without seeing the machine's; every typed field is stored, even when equal to it.
    The validation row keeps a snapshot of the machine values, so a later change re-opens the review."""
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
    if cause is not None and cause not in CAUSES:
        raise ValueError(f"cause must be one of {', '.join(CAUSES)}")
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
            cause=cause,
            blind=blind,
        )
        for name, value in changes.items()
    ]
    if validate:
        snapshot = json.dumps(machine_values(filing), ensure_ascii=False, sort_keys=True)
        rows.append(
            Correction(
                filing_key=filing_key,
                field=VALIDATED,
                value=snapshot,
                note=clean_note,
                author=author,
                created_at=at,
                blind=blind,
            )
        )
    with db.transaction():
        return [db.add_correction(c) for c in rows]


def export_corrections(db: Database) -> list[dict[str, object]]:
    """Active corrections as JSON rows (stable order), for `gold/corrections.jsonl`."""
    return [c.model_dump(mode="json", exclude={"id"}) for c in db.corrections()]


def import_corrections(db: Database, rows: Iterable[dict[str, object]]) -> int:
    """Insert rows not already present (same filing, field, author and time). Returns the number added."""
    seen = {(c.filing_key, c.field, c.author, c.created_at) for c in db.corrections()}
    added = 0
    with db.transaction():
        for row in rows:
            c = Correction.model_validate(row)
            if (c.filing_key, c.field, c.author, c.created_at) not in seen:
                db.add_correction(c)
                added += 1
    return added
