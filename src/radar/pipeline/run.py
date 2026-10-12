"""Whole-database steps used by the CLI: process filings, link, build projects. Each step is idempotent."""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime

from radar.classification.bayes import train, trusted_examples
from radar.domain.enums import TARGET_CATEGORIES
from radar.domain.models import Link
from radar.linking.linker import propose_links
from radar.linking.registry import commissioned
from radar.pipeline.changes import track_changes
from radar.pipeline.filings import Services, build_filing
from radar.pipeline.projects import build_projects
from radar.quality.arbiter import Arbiter, learn
from radar.quality.confidence import calibrate
from radar.quality.rulebook import LearnedRule, Rulebook
from radar.quality.shadow import diff_machine
from radar.store.db import Database, utcnow


@dataclass
class StepReport:
    processed: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    to_review: list[str] = field(default_factory=list)


def rule_filter(rules: tuple[LearnedRule, ...], arbiter: Arbiter) -> tuple[LearnedRule, ...]:
    """Drop adopted rules that reviews proved unreliable (see quality/rulebook.Rulebook.effective)."""
    seen = {s.removeprefix("rule:"): stats for (_, s), stats in arbiter.stats.items() if s.startswith("rule:")}
    return Rulebook(list(rules)).effective(seen)


def process_filings(db: Database, services: Services, only: set[str] | None = None) -> StepReport:
    """(Re)build every filing that has documents. One failing filing never stops the others.
    Source reliabilities and confidence calibration are re-learned from human reviews first, unless given."""
    report = StepReport()
    if services.arbiter is None or services.calibration is None or services.opinion is None:
        filings, corrections = db.filings(), db.corrections()
        arbiter = services.arbiter or learn(filings, corrections)
        services = replace(
            services,
            arbiter=arbiter,
            calibration=services.calibration or calibrate(filings, corrections),
            rules=rule_filter(services.rules, arbiter),
            opinion=services.opinion or train(trusted_examples(filings, services.labels or {})),
        )
    for key in db.filing_keys_with_documents():
        if only is not None and key not in only:
            continue
        try:
            previous = db.filing(key)
            filing = build_filing(key, db.documents(key), services, db.corrections(key))
        except Exception as exc:  # recorded and reported; the run continues
            report.failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        with db.transaction():
            db.upsert_filing(filing)
            if changed := diff_machine(previous, filing):  # recorded so unreviewed changes reach the review queue
                db.add_machine_changes(key, changed, filing.versions.get("extraction", ""))
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
    in_service = commissioned(filings, signals)
    links = propose_links(filings, signals) + [
        Link(
            a=min(key, s.key),
            b=max(key, s.key),
            reasons=(f"registre national : installation en service compatible ({s.payload['power_mw']} MW)",),
        )
        for key, s in in_service.items()
    ]
    built = build_projects(filings, signals, links, now=now, commissioned=in_service)
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
