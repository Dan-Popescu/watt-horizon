"""Human review (internal): list filings with machine vs current values, record corrections and validations.

Writes go through `submit_review`; the caller then rebuilds the filing (`process_filings(only=...)` + links)
so corrections are applied by the same code path as every nightly run.
"""

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime

from radar.api.views import (
    CheckView,
    CorrectionView,
    DocumentView,
    EvidenceView,
    FlagView,
    LinkReviewItem,
    LinkSide,
    ReviewItem,
)
from radar.classification.gold import GoldLabel
from radar.domain.enums import Category
from radar.domain.fields import field_values, machine_values
from radar.domain.models import LINK_VERDICTS, Correction, Filing, LinkDecision, Signal
from radar.domain.privacy import PersonScrubber
from radar.linking import review as link_review
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


def _side(key: str, filings: dict[str, Filing], signals: dict[str, Signal]) -> LinkSide:
    if f := filings.get(key):
        return LinkSide(
            key=key, kind="filing", label=f.number, category=f.classification.category.value, developer=f.developer,
            commune=f.location.commune, power_mw=f.power.mw, substation=f.substation,
            date=f.filed_on.isoformat() if f.filed_on else None, url=None,
        )  # fmt: skip
    s = signals.get(key)
    return LinkSide(
        key=key, kind="signal", label=s.title if s else key, category=s.category_hint.value if s and s.category_hint else None,
        developer=s.developer if s else None, commune=", ".join(s.communes) if s else None, power_mw=None,
        substation=None, date=s.observed_on.isoformat() if s and s.observed_on else None, url=s.url if s else None,
    )  # fmt: skip


def link_items(db: Database) -> list[LinkReviewItem]:
    """Links touching a target filing, undecided and most consequential first (linking/review.queue)."""
    filings = {f.key: f for f in db.filings()}
    signals = {s.key: s for s in db.signals()}
    pairs = link_review.queue(list(filings.values()), db.links())
    return [
        LinkReviewItem(
            a=_side(p.a, filings, signals),
            b=_side(p.b, filings, signals),
            reasons=list(p.reasons),
            verdict=p.verdict,
            merged_by_machine=p.merged_by_machine,
            priority=round(p.priority, 2),
        )
        for p in pairs
    ]


def submit_link_decision(
    db: Database, a: str, b: str, *, verdict: str, author: str, note: str = "", now: datetime | None = None
) -> LinkDecision:
    if verdict not in LINK_VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(LINK_VERDICTS)}")
    if not author.strip():
        raise ValueError("author is required")
    a, b = sorted((a, b))
    if a == b:
        raise ValueError("a link needs two different records")
    decision = LinkDecision(
        a=a, b=b, verdict=verdict, author=author.strip(), created_at=now or datetime.now(UTC), note=note
    )
    with db.transaction():
        return decision.model_copy(update={"id": db.add_link_decision(decision)})


def export_link_decisions(db: Database) -> list[dict[str, object]]:
    """Every link decision (history kept: the latest per pair wins), for `gold/link_decisions.jsonl`."""
    return [d.model_dump(mode="json", exclude={"id"}) for d in db.link_decisions()]


def import_link_decisions(db: Database, rows: Iterable[dict[str, object]]) -> int:
    seen = {(d.a, d.b, d.author, d.created_at) for d in db.link_decisions()}
    added = 0
    with db.transaction():
        for row in sorted(rows, key=lambda r: str(r["created_at"])):
            d = LinkDecision.model_validate(row)
            if (d.a, d.b, d.author, d.created_at) not in seen:
                db.add_link_decision(d)
                added += 1
    return added
