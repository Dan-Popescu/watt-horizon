"""Apply development-only consensus without leaking held-out references into training decisions."""

import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.domain.models import Correction
from radar.evaluation.campaign import Annotation, Campaign, check_documents
from radar.evaluation.campaign_review import exact_same, validate
from radar.pipeline.corrections import CORRECTABLE, approved_values, latest, validate_value
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.store.review_history import ReviewHistory


def apply_consensus(
    db: Database,
    campaign: Campaign,
    paths: list[Path],
    blobs: BlobStore,
    *,
    read: Callable[[str, bytes], PdfContent] | None = None,
    reviewed_seconds: float | None = None,
) -> dict[str, Any]:
    """Preflight the whole batch, then atomically record explicit observations; never a global validation.

    Existing human observations may agree, but conflicts require a separate human adjudication.
    Unknowns and withheld partitions are never applied or exposed as improvement examples.
    """
    if reviewed_seconds is not None and (not math.isfinite(reviewed_seconds) or reviewed_seconds < 0):
        raise ValueError("Declared review seconds must be finite and non-negative")
    report, references = validate(campaign, paths, blobs, read=read)
    annotation = report["annotation_fingerprint"]
    history = ReviewHistory(db.conn)
    if history.contains(campaign.fingerprint, annotation):
        return {"status": "already_applied", "applied": 0, "campaign": campaign.fingerprint}
    check_documents(db, campaign)
    rows: dict[str, list[Annotation]] = defaultdict(list)
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = Annotation.model_validate_json(line)
                if row.verified:
                    rows[row.filing_key].append(row)
    planned: list[tuple[Correction, list[dict[str, Any]]]] = []
    conflicts: list[dict[str, str]] = []
    withheld = skipped = 0
    now = datetime.now(UTC)
    for record in campaign.records:
        values = references.get(record.filing_key, {})
        if record.partition != "dev":
            withheld += len(values)
            continue
        current = latest(db.corrections(record.filing_key))
        approved = approved_values(current)
        for name, value in values.items():
            if name not in CORRECTABLE:
                skipped += 1
                continue
            try:
                normalized = validate_value(name, value)
            except ValueError:
                skipped += 1
                continue
            if name in approved:
                if not exact_same(approved[name], normalized):
                    conflicts.append({"filing_key": record.filing_key, "field": name})
                else:
                    skipped += 1
                continue
            supporters = [
                r
                for r in rows[record.filing_key]
                if r.fields[name].status in {"present", "absent"} and exact_same(r.fields[name].value, value)
            ]
            citations = [ev.model_dump(mode="json") for r in supporters if (ev := r.fields[name].evidence) is not None]
            planned.append(
                (
                    Correction(
                        filing_key=record.filing_key,
                        field=name,
                        value=normalized,
                        author=", ".join(sorted(r.reviewer for r in supporters)),
                        created_at=now,
                        note=f"Verified development consensus; campaign {campaign.fingerprint}; annotation {annotation}",
                        blind=True,
                        cause="other",
                    ),
                    citations,
                )
            )
    if conflicts:
        return {
            "error": "Campaign conflicts with existing human observations; adjudication required",
            "conflicts": conflicts,
            "applied": 0,
        }
    if not planned:
        return {
            "status": "no_new_development_observations",
            "applied": 0,
            "withheld_fields": withheld,
            "unsupported_or_unchanged": skipped,
        }
    with db.transaction():
        history.receipt(campaign.fingerprint, annotation, reviewed_seconds)
        for correction, citations in planned:
            correction_id = db.add_correction(correction)
            history.applied(
                campaign.fingerprint,
                annotation,
                correction.filing_key,
                correction.field,
                correction.value,
                correction_id,
                citations,
            )
    return {
        "status": "applied",
        "applied": len(planned),
        "withheld_fields": withheld,
        "unsupported_or_unchanged": skipped,
        "rebuild_required": True,
        "next": "radar run; radar errors; radar cases export; radar shadow --strict; radar eval --strict",
    }
