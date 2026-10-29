"""Durable incremental linking and project publication, with a full reference fallback.

Input fingerprints are O(records); unchanged runs do not load relations or rebuild projects.
Changed runs retain untouched relations and reuse projects whose exact membership and inputs stayed unchanged.
The caller serializes writers (the CLI and review routes use the dataset process lock).
"""

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from radar.domain.enums import SourceKind
from radar.domain.models import Link, Signal
from radar.linking.linker import propose_links
from radar.linking.registry import commissioned
from radar.linking.review import apply_link_decisions
from radar.pipeline.changes import track_changes
from radar.pipeline.projects import build_projects
from radar.store.db import SCHEMA_VERSION, Database, utcnow

STATE_NAME = "linking"
ALGORITHM_VERSION = 1
PACKAGE = Path(__file__).resolve().parents[1]


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: str
    filings: dict[str, str]
    signals: dict[str, str]
    decisions: dict[str, str]
    registry_locations: dict[str, str]
    commissioned: dict[str, str]
    links_revision: int
    projects_revision: int
    links_count: int
    projects_count: int


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _policy() -> str:
    paths = [
        "pipeline/linking_state.py",
        "pipeline/projects.py",
        "pipeline/changes.py",
        "linking/linker.py",
        "linking/registry.py",
        "linking/review.py",
        "linking/resolve.py",
        "domain/models.py",
        "domain/enums.py",
        "domain/ids.py",
        "domain/text.py",
    ]
    return _digest(
        [
            ALGORITHM_VERSION,
            SCHEMA_VERSION,
            [(name, hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest()) for name in paths],
        ]
    )


def _changes(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def _registry_links(matches: Mapping[str, Signal], only_keys: set[str] | None = None) -> list[Link]:
    return [
        Link(
            a=min(key, signal.key),
            b=max(key, signal.key),
            reasons=(f"registre national : installation en service compatible ({signal.payload['power_mw']} MW)",),
        )
        for key, signal in matches.items()
        if only_keys is None or key in only_keys or signal.key in only_keys
    ]


def _load(db: Database) -> _State | None:
    try:
        value = db.get_pipeline_state(STATE_NAME)
        return _State.model_validate(value) if value is not None else None
    except ValueError:
        return None


def _execute(db: Database, now: datetime | None, *, force_full: bool) -> dict[str, Any]:
    now = now or utcnow()
    if now.tzinfo is None:
        raise ValueError("Linking timestamps must include a timezone")
    filings, signals = db.filings(), db.signals()
    decisions = db.current_link_decisions()
    filings_by_key, signals_by_key = {f.key: f for f in filings}, {s.key: s for s in signals}
    filing_hashes = {f.key: _digest(f.model_dump(mode="json")) for f in filings}
    signal_hashes = {s.key: _digest(s.model_dump(mode="json")) for s in signals}
    decision_hashes = {_digest([a, b]): _digest(d.model_dump(mode="json")) for (a, b), d in decisions.items()}
    registry_locations = {s.key: s.payload.get("insee", "") for s in signals if s.source is SourceKind.ENERGY_REGISTRY}
    policy, previous = _policy(), _load(db)
    reason = "explicit-full" if force_full else "state-missing"
    valid = previous is not None and not force_full
    if valid and previous is not None:
        if previous.policy != policy:
            reason, valid = "policy-changed", False
        elif previous.links_revision != db.link_revision() or previous.projects_revision != db.project_revision():
            reason, valid = "outputs-changed", False
    changed_filings = _changes(previous.filings, filing_hashes) if valid and previous else set(filing_hashes)
    changed_signals = _changes(previous.signals, signal_hashes) if valid and previous else set(signal_hashes)
    changed_decisions = _changes(previous.decisions, decision_hashes) if valid and previous else set(decision_hashes)
    if valid and previous is not None and not (changed_filings or changed_signals or changed_decisions):
        with db.transaction():
            db.conn.execute("DELETE FROM pipeline_state WHERE name='linking_dirty'")
        return {
            "links": previous.links_count,
            "projects": previous.projects_count,
            "changes": {},
            "work": {"mode": "noop", "candidate_pairs": 0, "rebuilt_projects": 0, "rebuilt_links": 0},
        }

    old_projects = db.projects()
    work: dict[str, Any] = {"mode": "incremental" if valid else "full"}
    if not valid or previous is None:
        matches = commissioned(filings, signals)
        links = propose_links(filings, signals, metrics=work) + _registry_links(matches)
        links = apply_link_decisions(links, decisions)
        built = build_projects(filings, signals, links, now=now, commissioned=matches, metrics=work)
        work.update(reason=reason, rebuilt_links=len(links))
    else:
        old_links = db.links()
        affected = changed_filings | changed_signals
        # A changed decision may join or separate groups even if no matching key exists.
        for a, b in decisions:
            if _digest([a, b]) in changed_decisions:
                affected.update((a, b))
        for link in old_links:
            if _digest([link.a, link.b]) in changed_decisions:
                affected.update((link.a, link.b))
        # Registry additions and removals can change the closest installation in either the old or new commune.
        registry_places = {
            place
            for key in changed_signals
            for place in (previous.registry_locations.get(key), registry_locations.get(key))
            if place
        }
        affected.update(f.key for f in filings if f.location.insee_code in registry_places)
        affected_filings = [f for f in filings if f.key in affected]
        matches = {
            key: signals_by_key[signal_key]
            for key, signal_key in previous.commissioned.items()
            if key in filings_by_key and key not in affected and signal_key in signals_by_key
        }
        matches.update(commissioned(affected_filings, signals))
        retained = [link for link in old_links if link.a not in affected and link.b not in affected]
        rebuilt = propose_links(filings, signals, only_keys=affected, metrics=work) + _registry_links(matches, affected)
        links = apply_link_decisions(retained + rebuilt, decisions)
        # A signal's incident edge can disappear as well as appear. Both sets affect the owning projects.
        affected_projects = set(affected)
        for link in chain(old_links, links):
            if link.a in changed_signals or link.b in changed_signals:
                affected_projects.update((link.a, link.b))
        built = build_projects(
            filings,
            signals,
            links,
            now=now,
            commissioned=matches,
            previous=old_projects,
            affected_keys=affected_projects,
            metrics=work,
        )
        work.update(
            rebuilt_links=sum(link.a in affected or link.b in affected for link in links),
            changed_records=len(changed_filings) + len(changed_signals),
            changed_decisions=len(changed_decisions),
        )
    projects, changes = track_changes(old_projects, built, now)
    with db.transaction():
        db.replace_links(links)
        db.replace_projects(projects)
        db.add_changes(changes)
        state = _State(
            policy=policy,
            filings=filing_hashes,
            signals=signal_hashes,
            decisions=decision_hashes,
            registry_locations=registry_locations,
            commissioned={key: signal.key for key, signal in matches.items()},
            links_revision=db.link_revision(),
            projects_revision=db.project_revision(),
            links_count=len(links),
            projects_count=len(projects),
        )
        db.set_pipeline_state(STATE_NAME, state.model_dump(mode="json"))
        db.conn.execute("DELETE FROM pipeline_state WHERE name='linking_dirty'")
    return {
        "links": len(links),
        "projects": len(projects),
        "changes": dict(Counter(change.kind.value for change in changes)),
        "work": work,
    }


def incremental_link_and_build(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Publish only affected relations/groups, or fall back to the full reference when cache safety is unknown."""
    return _execute(db, now, force_full=False)


def full_link_and_build(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Reference rebuild and recovery path; the same atomic journal and persistent state are produced."""
    return _execute(db, now, force_full=True)
