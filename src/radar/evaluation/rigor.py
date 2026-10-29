"""Measurements that stay honest as data grows: per-stratum scores, confidence calibration, blind accuracy,
and the screen's recall estimated from the random audit sample. All rates come with a 95 % Wilson interval.
"""

from collections.abc import Iterable
from typing import Any

from radar.domain.enums import TARGET_CATEGORIES, Category
from radar.domain.fields import machine_values
from radar.domain.models import Correction, Filing
from radar.evaluation.fields import compare
from radar.evaluation.gold import FieldGold, actual_fields
from radar.evaluation.stats import wilson
from radar.pipeline.corrections import VALIDATED, latest, reviewed_references

STRATA = ("form_layout", "read", "source", "template")


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"rate": round(k / n, 3) if n else None, "ci95": wilson(k, n), "n": n}


def by_stratum(filings: Iterable[Filing], gold: dict[str, FieldGold]) -> dict[str, dict[str, dict[str, Any]]]:
    """Field accuracy pooled over all verified fields, per value of each stratum (a weak layout shows up here)."""
    counts: dict[str, dict[str, list[int]]] = {key: {} for key in STRATA}
    for f in filings:
        reference = gold.get(f.number)
        if reference is None or not reference.verified:
            continue
        actual = actual_fields(f)
        good = sum(compare(v, actual.get(k)) in {"correct", "absent"} for k, v in reference.expected.items())
        for key in STRATA:
            slot = counts[key].setdefault(f.strata.get(key, "?"), [0, 0])
            slot[0] += good
            slot[1] += len(reference.expected)
    return {key: {value: _rate(k, n) for value, (k, n) in sorted(values.items())} for key, values in counts.items()}


def calibration(filings: Iterable[Filing], numbers_with_errors: set[str], reviewed: set[str]) -> dict[str, Any]:
    """Share of reviewed filings with at least one error, per MACHINE confidence. "high" should be near 0."""
    counts: dict[str, list[int]] = {}
    for f in filings:
        if f.number not in reviewed:
            continue
        level = (f.machine.confidence if f.machine and f.machine.confidence else f.confidence.value) or "?"
        slot = counts.setdefault(level, [0, 0])
        slot[0] += f.number in numbers_with_errors
        slot[1] += 1
    return {level: _rate(k, n) for level, (k, n) in sorted(counts.items())}


def reviewed_errors(filings: Iterable[Filing], corrections: Iterable[Correction]) -> tuple[set[str], set[str]]:
    """Aggregate checked outcomes before hiding held-out examples from diagnostic output."""
    records = list(filings)
    labels, references = reviewed_references(records, corrections)
    reviewed = set(labels) | set(references)
    wrong = set()
    for filing in records:
        reference = references.get(filing.number)
        actual = actual_fields(filing)
        if reference and any(
            compare(value, actual.get(name)) not in {"correct", "absent"} for name, value in reference.expected.items()
        ):
            wrong.add(filing.number)
        label = labels.get(filing.number)
        category = filing.machine.classification.category if filing.machine else filing.classification.category
        if label and category != label.category:
            wrong.add(filing.number)
    return wrong, reviewed


def blind_accuracy(filings: Iterable[Filing], corrections: Iterable[Correction]) -> dict[str, dict[str, Any]]:
    """Machine accuracy per field against values typed blind (the reviewer never saw the machine's)."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        if c.blind and c.field != VALIDATED:
            by_key.setdefault(c.filing_key, []).append(c)
    counts: dict[str, list[int]] = {}
    for f in filings:
        machine = machine_values(f)
        for name, c in latest(by_key.get(f.key, [])).items():
            if name in machine:
                slot = counts.setdefault(name, [0, 0])
                slot[0] += compare(c.value, machine[name]) in {"correct", "absent"}
                slot[1] += 1
    return {name: _rate(k, n) for name, (k, n) in sorted(counts.items())}


def recall_estimate(
    filings: list[Filing], corrections: Iterable[Correction], listing: dict[str, int]
) -> dict[str, Any]:
    """Screen recall from the random audit sample: share of targets among reviewed random-audit filings,
    extrapolated to all rejected items. Needs listing_items (collect runs) and reviewed audit filings."""
    labels, _ = reviewed_references(filings, corrections)
    audited = [
        f
        for f in filings
        if f.number in labels and any(g.code == "audit_sample" and g.detail == "random" for g in f.flags)
    ]
    kept = [f for f in filings if not any(g.code == "audit_sample" for g in f.flags)]
    kept_targets = sum(labels[f.number].category in TARGET_CATEGORIES for f in kept if f.number in labels)
    found = sum(labels[f.number].category in TARGET_CATEGORIES for f in audited)
    rejected = listing.get("rejected", 0)
    out: dict[str, Any] = {
        "rejected_items": rejected,
        "random_audited_reviewed": len(audited),
        "targets_found": found,
        "kept_unverified": sum(f.number not in labels for f in kept),
    }
    if listing.get("audit_policy_version") != 2:
        return {**out, "recall": None, "why": "legacy_random_population_not_representative"}
    if listing.get("audit_random", 0) != len(audited):
        return {**out, "recall": None, "why": "random_sample_incomplete"}
    if out["kept_unverified"]:
        return {**out, "recall": None, "why": "kept_targets_unverified"}
    interval = wilson(found, len(audited))
    if not rejected or interval is None:
        return {**out, "recall": None, "why": "il faut des listes collectées et des dossiers d'audit aléatoire relus"}
    missed = [rejected * share for share in (found / len(audited), interval[1], interval[0])]
    recall = [kept_targets / (kept_targets + m) if kept_targets + m else None for m in missed]
    return {**out, "kept_targets": kept_targets, "recall": recall[0], "recall_ci95": (recall[1], recall[2])}


def undecided_share(filings: Iterable[Filing]) -> float | None:
    items = list(filings)
    if not items:
        return None
    return round(
        sum(f.classification.category in {Category.UNCLASSIFIED, Category.NEEDS_REVIEW} for f in items) / len(items), 3
    )
