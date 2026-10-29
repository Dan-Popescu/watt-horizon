"""Whole-database steps used by the CLI: process filings, link, build projects. Each step is idempotent."""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from functools import partial
from itertools import islice
from pathlib import Path

from radar.classification.bayes import train, trusted_examples
from radar.domain.enums import TARGET_CATEGORIES
from radar.domain.models import Filing, Link
from radar.evaluation.partitions import PartitionManifest, ensure_partitions, load_manifest
from radar.linking.linker import propose_links
from radar.linking.registry import commissioned
from radar.linking.review import apply_link_decisions
from radar.pipeline.changes import track_changes
from radar.pipeline.filings import Services, build_filing
from radar.pipeline.fingerprint import (
    code_fingerprint,
    corrections_fingerprint,
    digest,
    filing_fingerprint,
    git_commit,
    processing_fingerprint,
)
from radar.pipeline.projects import build_projects
from radar.quality.arbiter import Arbiter, learn
from radar.quality.confidence import calibrate
from radar.quality.rulebook import LearnedRule, Rulebook
from radar.quality.shadow import diff_machine
from radar.store.db import Database, utcnow


@dataclass
class StepReport:
    processed: int = 0
    unchanged: int = 0  # skipped: same documents, code, gold files, corrections and learned state as last build
    failed: dict[str, str] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    to_review: list[str] = field(default_factory=list)


def rule_filter(rules: tuple[LearnedRule, ...], arbiter: Arbiter) -> tuple[LearnedRule, ...]:
    """Drop adopted rules that reviews proved unreliable (see quality/rulebook.Rulebook.effective)."""
    seen = {s.removeprefix("rule:"): stats for (_, s), stats in arbiter.stats.items() if s.startswith("rule:")}
    return Rulebook(list(rules)).effective(seen)


def process_filings(
    db: Database, services: Services, only: set[str] | None = None, *, full: bool = False
) -> StepReport:
    """(Re)build every filing that has documents. One failing filing never stops the others.
    Source reliabilities and confidence calibration are re-learned from human reviews first, unless given.
    Incremental: a filing whose input fingerprint is unchanged is skipped (unless `full` or listed in `only`)."""
    report = StepReport()
    partitioned, manifest = assign_partitions(db, services.partitions_path, services.partitions_test_after)
    if services.arbiter is None or services.calibration is None or services.opinion is None:
        filings, corrections = partitioned, db.corrections()
        arbiter = services.arbiter or learn(filings, corrections)
        services = replace(
            services,
            arbiter=arbiter,
            calibration=services.calibration or calibrate(filings, corrections),
            rules=rule_filter(services.rules, arbiter),
            opinion=services.opinion or train(trusted_examples(filings, services.labels or {})),
        )
    state = _state(db, services) if services.fingerprint else None
    may_skip = only is None and not full
    keys = iter(db.iter_filing_keys_with_documents())
    # Keep the work list bounded independently of the total dataset and PDF count.
    while batch_keys := list(islice(keys, 64)):
        todo = []
        for key in batch_keys:
            if only is not None and key not in only:
                continue
            previous, documents = db.filing(key), db.documents(key)
            corrections = db.corrections(key)
            fingerprint = (
                digest(filing_fingerprint(state, documents), corrections_fingerprint(corrections)) if state else None
            )
            if may_skip and fingerprint and previous and previous.versions.get("input") == fingerprint:
                report.unchanged += 1
                continue
            todo.append((key, previous, documents, corrections, fingerprint))
        services.reader.prefetch(
            [
                (d.id, partial(services.blobs.get, d.id, max_bytes=services.reader.max_bytes))
                for _, _, docs, _, _ in todo
                for d in docs
            ]
        )
        for key, previous, documents, corrections, fingerprint in todo:
            try:
                filing = build_filing(key, documents, services, corrections)
            except Exception as exc:  # recorded and reported; the run continues
                report.failed[key] = f"{type(exc).__name__}: {exc}"
                continue
            if services.llm_queue is not None and services.llm_queue.waiting(key):
                fingerprint = None  # a response is still pending or blocked: do not seal this output
            trace = {"code": code_fingerprint(), **({"git": commit} if (commit := git_commit()) else {})}
            strata = dict(filing.strata)
            if manifest and key in manifest.assignments:
                strata.update(
                    evaluation_partition=manifest.assignments[key],
                    evaluation_group=manifest.groups[key],
                    evaluation_manifest=manifest.fingerprint,
                )
            filing = filing.model_copy(
                update={
                    "versions": filing.versions | trace | ({"input": fingerprint} if fingerprint else {}),
                    "strata": strata,
                }
            )
            with db.transaction():
                db.upsert_filing(filing)
                if changed := diff_machine(previous, filing):
                    db.add_machine_changes(key, changed, filing.versions.get("extraction", ""))
            report.processed += 1
            cat = filing.classification.category.value
            report.by_category[cat] = report.by_category.get(cat, 0) + 1
            if filing.confidence.value == "review" and filing.classification.category in TARGET_CATEGORIES:
                report.to_review.append(key)
    assign_partitions(db, services.partitions_path, services.partitions_test_after)
    return report


def assign_partitions(
    db: Database,
    path: Path | None,
    test_after: date | None = None,
) -> tuple[list[Filing], PartitionManifest | None]:
    """Freeze related records together before any learned policy uses human observations."""
    filings = db.filings()
    if path is None:
        return filings, None
    previous = load_manifest(path)
    first_seen: dict[str, date] | None = None
    if test_after is not None or (previous and previous.test_after):
        first_seen = {}
        for filing in filings:
            if previous and filing.key in previous.assignments:
                continue
            observed = [d.fetched_at.astimezone(UTC).date() for d in db.documents(filing.key)]
            if observed:
                first_seen[filing.key] = min(observed)
    annotated, manifest = ensure_partitions(
        path,
        filings,
        projects=db.projects(),
        links=db.links(),
        first_seen=first_seen,
        test_after=test_after,
    )
    with db.transaction():
        for old, new in zip(filings, annotated, strict=True):
            if old.strata != new.strata:
                db.upsert_filing(new)
    return annotated, manifest


def _state(db: Database, services: Services) -> str:
    """Everything shared by all builds: setup, code, corrections and what was learned from them."""
    opinion = services.opinion
    return digest(
        services.fingerprint,
        processing_fingerprint(),
        sorted((list(k), list(v)) for k, v in (services.arbiter.stats if services.arbiter else {}).items()),
        services.calibration.bins if services.calibration else None,
        sorted((list(key), value) for key, value in services.calibration.by_field.items())
        if services.calibration
        else None,
        [
            sorted(opinion.docs.items()),
            sorted((category, sorted(counts.items())) for category, counts in opinion.counts.items()),
            sorted(opinion.vocabulary),
            sorted(opinion.absent.items()),
            opinion.loo_correct,
            opinion.loo_total,
        ]
        if opinion
        else None,
        [r.id for r in services.rules],
    )


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
    links = apply_link_decisions(links, db.current_link_decisions())
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
