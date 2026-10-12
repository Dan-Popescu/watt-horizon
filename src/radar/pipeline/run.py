"""Whole-database steps used by the CLI: process filings, link, build projects. Each step is idempotent."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from radar.domain.enums import TARGET_CATEGORIES
from radar.linking.linker import propose_links
from radar.pipeline.changes import track_changes
from radar.pipeline.corrections import apply_corrections, with_machine
from radar.pipeline.filings import Services, build_filing
from radar.pipeline.projects import build_projects
from radar.store.db import Database, utcnow


@dataclass
class StepReport:
    processed: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    to_review: list[str] = field(default_factory=list)


def process_filings(db: Database, services: Services, only: set[str] | None = None) -> StepReport:
    """(Re)build every filing that has documents. One failing filing never stops the others."""
    report = StepReport()
    for key in db.filing_keys_with_documents():
        if only is not None and key not in only:
            continue
        try:
            machine = with_machine(build_filing(key, db.documents(key), services))
            filing = apply_corrections(machine, db.corrections(key), services.lookups)
        except Exception as exc:  # recorded and reported; the run continues
            report.failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        with db.transaction():
            db.upsert_filing(filing)
        report.processed += 1
        cat = filing.classification.category.value
        report.by_category[cat] = report.by_category.get(cat, 0) + 1
        if filing.confidence.value == "review" and filing.classification.category in TARGET_CATEGORIES:
            report.to_review.append(key)
    return report


def link_and_build(db: Database, now: datetime | None = None) -> dict[str, object]:
    """Propose links, rebuild projects and append the change log, in one transaction."""
    now = now or utcnow()
    filings, signals = db.filings(), db.signals()
    links = propose_links(filings, signals)
    built = build_projects(filings, signals, links, now=now)
    projects, changes = track_changes(db.projects(), built, now)
    with db.transaction():
        db.replace_links(links)
        db.replace_projects(projects)
        db.add_changes(changes)
    return {
        "links": len(links),
        "projects": len(projects),
        "changes": dict(Counter(c.kind.value for c in changes)),
    }
