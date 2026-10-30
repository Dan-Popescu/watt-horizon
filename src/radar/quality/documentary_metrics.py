"""Evidence-based operational diagnostics for the documentary quality loop.

Listing counts have no territorial column and deliberately retain their global scope.
Reviewed random audits are descriptive: incomplete or prioritised response is never an unbiased recall estimate.
"""

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from radar.domain.enums import TARGET_CATEGORIES, SourceKind
from radar.domain.models import Filing
from radar.evaluation.campaign import current_links
from radar.evaluation.campaign_review import exact_same
from radar.evaluation.gold import actual_fields
from radar.evaluation.partitions import build_manifest
from radar.evaluation.stats import wilson
from radar.pipeline.corrections import reviewed_references
from radar.store import diagnostics
from radar.store.db import Database
from radar.store.discovery import DiscoveryStore
from radar.store.quality_queries import (
    coverage_scopes,
    discovery_attempt_counts,
    dossier_counts,
    listing_counts,
    random_audit_items,
)
from radar.store.review_history import ReviewHistory

SOURCE_NAMES = {
    "dreal": SourceKind.DREAL_CASE_BY_CASE.value,
    "mrae": SourceKind.MRAE_OPINION.value,
    "prefectures": SourceKind.PREFECTURE.value,
}
READING_COUNTERS = (
    "pages",
    "unread_pages",
    "ocr_attempts",
    "ocr_native_pages",
    "ocr_cache_pages",
    "table_pages_attempted",
    "tables",
    "table_rows",
    "table_errors",
    "useful_unread_pages",
    "deepening_pages",
    "format_errors",
    "uncertain_segments",
)
REQUIRED_TARGET_FIELDS = ("developer", "commune", "power_mw", "filed_on", "review_decision")


def _model_cost(path: Path | None) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "status": "unavailable",
        "scope": "entire_local_model_journal",
        "priced_usage_usd": None,
        "known_priced_usage_usd": None,
        "unpriced_attempts": None,
        "attempts": None,
        "jobs": {},
        "caveat": "Coût des usages au tarif enregistré, hors hébergement et facture fournisseur.",
    }
    if path is None or not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            total, priced, known, reserved, negative = connection.execute(
                "SELECT COUNT(*),COUNT(actual_micros),SUM(actual_micros),SUM(reserved_micros),"
                "SUM(actual_micros<0) FROM attempts"
            ).fetchone()
            states = dict(connection.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"))
    except (sqlite3.Error, OSError, ValueError):
        return empty
    if negative:
        return empty
    return empty | {
        "status": "complete" if total and total == priced else "partial" if total else "no_attempts",
        "attempts": total,
        "unpriced_attempts": total - priced,
        "priced_usage_usd": known / 1_000_000 if total and total == priced else None,
        "known_priced_usage_usd": known / 1_000_000 if priced else None,
        "reserved_usd": reserved / 1_000_000 if reserved is not None else None,
        "jobs": states,
    }


def _freshness(db: Database, region: str | None) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC)
    for short_source, row in coverage_scopes(db.conn, region):
        source = SOURCE_NAMES.get(short_source, short_source)
        stats = output.setdefault(
            source, {"observed_scopes": 0, "states": {}, "stale_complete_scopes": 0, "unknown_freshness_scopes": 0}
        )
        stats["observed_scopes"] += 1
        state = row.get("state", "unknown")
        stats["states"][state] = stats["states"].get(state, 0) + 1
        at = row.get("completed_at") if state == "complete" else row.get("updated_at")
        try:
            when = datetime.fromisoformat(at) if isinstance(at, str) else None
            if when is None or when.tzinfo is None:
                raise ValueError("Missing UTC observation")
            if state == "complete" and now - when > timedelta(days=7):
                stats["stale_complete_scopes"] += 1
        except ValueError:
            stats["unknown_freshness_scopes"] += 1
    return output


def _reading(records: list[Filing]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    known: Counter[str] = Counter()
    contextual: Counter[str] = Counter()
    for filing in records:
        for name in READING_COUNTERS:
            value = filing.strata.get(f"reading_{name}")
            try:
                number = int(value) if value is not None else None
            except ValueError:
                number = None
            if number is not None and number >= 0:
                totals[name] += number
                known[name] += 1
        for observation in filing.quantity_observations:
            contextual["observations"] += 1
            contextual["eligible" if observation.eligible else "requires_context"] += 1
            contextual[f"asset:{observation.asset}"] += 1
    return {
        "scope": "latest_filing_reading_observations; shared PDFs may repeat",
        "filings": len(records),
        "counters": {name: totals[name] if known[name] else None for name in READING_COUNTERS},
        "filings_with_counters": dict(known),
        "quantity_context": dict(contextual),
        "unread_document_ids": len({doc for filing in records for doc in filing.unread_scans}),
    }


def build_quality_loop(db: Database, *, jobs_path: Path | None = None, region: str | None = None) -> dict[str, Any]:
    """Read-only, aggregate-only diagnostics; missing evidence remains unavailable."""
    population = db.filings()
    grouping = build_manifest(population, projects=db.projects(), links=current_links(db))
    records = [filing for filing in population if region is None or filing.region == region]
    document_keys = set(db.filing_keys_with_documents())
    eligible_keys = {filing.key for filing in records} | {
        key for key in document_keys if region is None or key.split(":", 2)[1] == region
    }
    labels, fields = reviewed_references(population, db.corrections())
    counts = listing_counts(db.conn)
    sources: dict[str, dict[str, Any]] = {}
    for source, values in counts.items():
        sources[source] = {
            "listings": values,
            "listing_scope": "all_regions",
            "random_audit": {"mapped_downloaded": 0, "unmapped": 0, "outside_region": 0, "reviewed": 0, "targets": 0},
        }
    audit_groups: dict[str, dict[str, bool]] = defaultdict(dict)
    for source, keys in random_audit_items(db.conn):
        item = sources[source]["random_audit"]
        relevant_keys = {key for key in keys if key.startswith(source + ":")}
        if not relevant_keys:
            item["unmapped"] += 1
            continue
        if region is not None and not relevant_keys & eligible_keys:
            item["outside_region"] += 1
            continue
        selected = relevant_keys & eligible_keys
        downloaded = selected & document_keys
        item["mapped_downloaded"] += bool(downloaded)
        # Every mapped case must have an explicit human category, not machine confidence or model agreement.
        if not selected or not downloaded or not all(key in labels for key in selected):
            continue
        target = any(labels[key].category in TARGET_CATEGORIES for key in selected)
        item["reviewed"] += 1
        item["targets"] += target
        for key in selected:
            group = grouping.groups[key]
            audit_groups[source][group] = (
                audit_groups[source].get(group, False) or labels[key].category in TARGET_CATEGORIES
            )
    for source, item in sources.items():
        audit = item["random_audit"]
        selected = item["listings"]["random_selected"]
        groups = audit_groups[source]
        audit.update(
            {
                "selected_global": selected,
                "not_human_reviewed_global": selected - audit["reviewed"],
                "human_response_share_global": audit["reviewed"] / selected if selected else None,
                "target_share_in_reviewed_items": audit["targets"] / audit["reviewed"] if audit["reviewed"] else None,
                "reviewed_project_groups": len(groups),
                "target_project_groups": sum(groups.values()),
                "scope": "reviewed_random_audits_only",
                "population_recall": None,
                "caveat": "Relecture incomplète ou priorisée : aucun rappel global ni échantillon sans biais déduit.",
            }
        )
    cells: dict[tuple[str, str, str], dict[str, bool]] = defaultdict(dict)
    observations: Counter[tuple[str, str, str]] = Counter()
    complete_targets: dict[str, bool] = {}
    for filing in records:
        reference = fields.get(filing.key)
        group = grouping.groups[filing.key]
        partition = grouping.assignments[filing.key]
        usable = (
            filing.key in labels
            and labels[filing.key].category in TARGET_CATEGORIES
            and reference is not None
            and all(reference.expected.get(name) is not None for name in REQUIRED_TARGET_FIELDS)
        )
        complete_targets[group] = complete_targets.get(group, True) and usable
        if reference is None or filing.machine is None:
            continue
        actual = actual_fields(filing)
        for name, expected in reference.expected.items():
            cell = (filing.source.value, partition, name)
            observations[cell] += 1
            correct = exact_same(expected, actual.get(name))
            cells[cell][group] = cells[cell].get(group, True) and correct
    for (source, split_name, name), groups in cells.items():
        stat = sources.setdefault(source, {})
        stat.setdefault("verified_fields", {}).setdefault(split_name, {})[name] = {
            "observations": observations[(source, split_name, name)],
            "independent_groups": len(groups),
            "exact_group_accuracy": sum(groups.values()) / len(groups),
            "group_ci95": wilson(sum(groups.values()), len(groups)),
        }
    for source, values in dossier_counts(db.conn, region).items():
        sources.setdefault(source, {})["dossiers"] = values
    for source, values in _freshness(db, region).items():
        sources.setdefault(source, {})["coverage"] = values
    cost = _model_cost(jobs_path)
    complete_n = sum(complete_targets.values())
    cost["human_complete_target_groups"] = complete_n
    cost["priced_usage_per_complete_target_group_usd"] = (
        cost["priced_usage_usd"] / complete_n
        if region is None and complete_n and cost["priced_usage_usd"] is not None
        else None
    )
    cost["unit_cost_scope"] = "cumulative journal usage / fully human-annotated target groups; not a cloud invoice"
    receipts = ReviewHistory(db.conn).summary()
    receipts["scope"] = "all_regions; reviewer-declared duration, not measured active attention"
    discovery_status = DiscoveryStore(db.conn).status(region, limit=1)
    discovery = {
        name: discovery_status[name]
        for name in (
            "queries",
            "leads",
            "provider_requests",
            "provider_pages",
            "provider_results",
            "model_calls",
            "provider_cost_usd",
            "cost_scope",
        )
    }
    discovery["attempts"] = discovery_attempt_counts(db.conn, region)
    discovery["request_scope"] = "persisted physical request admissions, including redirects and retries"
    runs = db.runs("collect:", limit=100)
    durations = [(run["finished_at"] - run["started_at"]).total_seconds() for run in runs if run["finished_at"]]
    evaluation = db.runs("eval", limit=1)
    last_report = evaluation[0]["stats"].get("report", {}) if evaluation else {}
    raw_gate = last_report.get("gate") if isinstance(last_report, dict) else None
    gate = (
        {"status": raw_gate.get("status"), "failure_count": len(raw_gate.get("failures", []))}
        if isinstance(raw_gate, dict)
        else None
    )
    run_sources: dict[str, dict[str, Any]] = {}
    for run in runs:
        source = SOURCE_NAMES.get(run["kind"].removeprefix("collect:"), run["kind"].removeprefix("collect:"))
        totals = run_sources.setdefault(source, {"completed": 0, "unfinished": 0, "duration_sum_s": None})
        if run["finished_at"]:
            totals["completed"] += 1
            duration = (run["finished_at"] - run["started_at"]).total_seconds()
            totals["duration_sum_s"] = (totals["duration_sum_s"] or 0) + duration
        else:
            totals["unfinished"] += 1
    return {
        "region": region,
        "sources": sources,
        "reading": _reading(records),
        "model_cost": cost,
        "human_feedback": receipts,
        "discovery": discovery,
        "human_complete_target_groups": complete_n,
        "latest_evaluation_gate": gate,
        "quality_measured": bool(cells),
        "collection_runs": {
            "scope": "last_100_global_collect_runs",
            "completed": len(durations),
            "unfinished": sum(run["finished_at"] is None for run in runs),
            "duration_sum_s": sum(durations) if durations else None,
            "by_source": run_sources,
        },
        "group_scope": "current_project_and_shared_document_groups; diagnostic_only",
        "caveat": "Mesures sur les observations disponibles, sans certification ni exhaustivité globale.",
    }


def refresh_quality_loop(db: Database, *, jobs_path: Path | None = None, region: str | None = None) -> dict[str, Any]:
    """Worker entry point: compute and persist one revision-bound aggregate snapshot."""
    return diagnostics.refresh(
        db,
        "documentary:" + (region or "all"),
        lambda: build_quality_loop(db, jobs_path=jobs_path, region=region),
        scope={"region": region, "kind": "documentary"},
        external=lambda: diagnostics.external_token(jobs_path) if jobs_path else "",
    )


def read_quality_loop(db: Database, *, jobs_path: Path | None = None, region: str | None = None) -> dict[str, Any]:
    """API entry point: unavailable and stale are explicit; never run the graph diagnostics here."""
    return diagnostics.read(
        db,
        "documentary:" + (region or "all"),
        scope={"region": region, "kind": "documentary"},
        external=diagnostics.external_token(jobs_path) if jobs_path else "",
    )
