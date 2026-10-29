"""Paired, offline comparisons of raw machine snapshots on frozen human references."""

from collections import defaultdict
from pathlib import Path
from typing import Any

from radar.domain.fields import Value
from radar.evaluation.campaign import Campaign, FrozenModel, check_documents, digest
from radar.evaluation.campaign_review import exact_same
from radar.evaluation.fields import compare
from radar.evaluation.partitions import atomic_json
from radar.evaluation.stats import wilson
from radar.store.db import Database


class Prediction(FrozenModel):
    filing_key: str
    document_ids: tuple[str, ...]
    values: dict[str, Value]
    classification_method: str
    versions: dict[str, str]


class Snapshot(FrozenModel):
    version: int = 1
    campaign_fingerprint: str
    name: str
    predictions: tuple[Prediction, ...]
    fingerprint: str = ""

    def digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"fingerprint"}))


def snapshot(db: Database, campaign: Campaign, path: Path, name: str) -> dict[str, Any]:
    if path.exists() or not name.strip():
        raise ValueError("Snapshot name is required and destination must not exist")
    filings = check_documents(db, campaign)
    predictions = []
    for record in campaign.records:
        filing = filings[record.filing_key]
        if filing.machine is None:
            raise ValueError("Campaign snapshots require raw machine output; corrected values are forbidden")
        predictions.append(
            Prediction(
                filing_key=filing.key,
                document_ids=tuple(sorted(filing.document_ids)),
                values=filing.machine.values | {"category": filing.machine.classification.category.value},
                classification_method=filing.machine.classification.method,
                versions=filing.versions,
            )
        )
    artifact = Snapshot(campaign_fingerprint=campaign.fingerprint, name=name, predictions=tuple(predictions))
    artifact = artifact.model_copy(update={"fingerprint": artifact.digest()})
    atomic_json(path, artifact.model_dump(mode="json"))
    return {"path": str(path), "name": name, "filings": len(predictions), "fingerprint": artifact.fingerprint}


def load_snapshot(path: Path, campaign: Campaign) -> Snapshot:
    snapshot = Snapshot.model_validate_json(path.read_text(encoding="utf-8"))
    if snapshot.version != 1 or snapshot.fingerprint != snapshot.digest():
        raise ValueError("Snapshot version or fingerprint changed")
    if snapshot.campaign_fingerprint != campaign.fingerprint:
        raise ValueError("Snapshots must belong to the same campaign and source documents")
    predictions = {p.filing_key: p for p in snapshot.predictions}
    if len(predictions) != len(snapshot.predictions) or set(predictions) != {r.filing_key for r in campaign.records}:
        raise ValueError("Snapshot must contain exactly one raw output for every campaign filing")
    for record in campaign.records:
        if set(predictions[record.filing_key].document_ids) != {d.id for d in record.documents}:
            raise ValueError("Snapshot document inventory does not match campaign")
    return snapshot


def _independent_category(method: str) -> bool:
    return bool(method) and not any(word in method.lower() for word in ("gold", "human", "reference"))


def comparison(
    campaign: Campaign,
    references: dict[str, dict[str, Value]],
    baseline: Snapshot,
    candidate: Snapshot,
) -> dict[str, Any]:
    """Report exact and legacy-tolerant measures separately, with paired independent-group outcomes."""
    before = {p.filing_key: p for p in baseline.predictions}
    after = {p.filing_key: p for p in candidate.predictions}
    cells: dict[tuple[str, str], list[tuple[str, bool, bool, str, str, bool, bool]]] = defaultdict(list)
    details = []
    excluded_categories = 0
    for record in campaign.records:
        key = record.filing_key
        for name, expected in references.get(key, {}).items():
            if name == "category" and not all(
                _independent_category(p.classification_method) for p in (before[key], after[key])
            ):
                excluded_categories += 1
                continue
            a, b = before[key].values.get(name), after[key].values.get(name)
            exact_a, exact_b = exact_same(expected, a), exact_same(expected, b)
            outcome_a, outcome_b = compare(expected, a), compare(expected, b)
            tolerant_a, tolerant_b = outcome_a in {"correct", "absent"}, outcome_b in {"correct", "absent"}
            cells[(record.partition, name)].append(
                (record.group, exact_a, exact_b, outcome_a, outcome_b, tolerant_a, tolerant_b)
            )
            if record.partition == "dev" and (not exact_a or not exact_b):
                details.append(
                    {
                        "filing_key": key,
                        "field": name,
                        "baseline": outcome_a,
                        "candidate": outcome_b,
                        "baseline_exact": exact_a,
                        "candidate_exact": exact_b,
                        "expected": expected,
                        "baseline_value": a,
                        "candidate_value": b,
                    }
                )
    partitions: dict[str, dict[str, Any]] = defaultdict(dict)
    for (partition, name), values in sorted(cells.items()):
        groups: dict[str, list[bool]] = {}
        outcomes_a: dict[str, int] = defaultdict(int)
        outcomes_b: dict[str, int] = defaultdict(int)
        for group, exact_a, exact_b, status_a, status_b, tolerant_a, tolerant_b in values:
            combined = groups.setdefault(group, [True, True, True, True])
            for i, success in enumerate((exact_a, exact_b, tolerant_a, tolerant_b)):
                combined[i] = combined[i] and success
            outcomes_a[status_a] += 1
            outcomes_b[status_b] += 1
        n, independent_n = len(values), len(groups)
        metrics = []
        for i, outcomes in ((0, outcomes_a), (1, outcomes_b)):
            correct = sum(item[1] if i == 0 else item[2] for item in values)
            group_correct = sum(item[i] for item in groups.values())
            tolerant_group_correct = sum(item[i + 2] for item in groups.values())
            metrics.append(
                {
                    "n": n,
                    "exact_accuracy": correct / n,
                    "business_tolerance_accuracy": (outcomes.get("correct", 0) + outcomes.get("absent", 0)) / n,
                    "outcomes": dict(outcomes),
                    "independent_n": independent_n,
                    "independent_exact_accuracy": group_correct / independent_n,
                    "independent_exact_ci95": wilson(group_correct, independent_n),
                    "independent_tolerant_accuracy": tolerant_group_correct / independent_n,
                    "independent_tolerant_ci95": wilson(tolerant_group_correct, independent_n),
                }
            )
        partitions[partition][name] = {
            "baseline": metrics[0],
            "candidate": metrics[1],
            "paired_groups": {
                "improved_exact": sum(not item[0] and item[1] for item in groups.values()),
                "regressed_exact": sum(item[0] and not item[1] for item in groups.values()),
                "unchanged_exact": sum(item[0] == item[1] for item in groups.values()),
            },
        }
    return {
        "campaign_fingerprint": campaign.fingerprint,
        "baseline": {"name": baseline.name, "fingerprint": baseline.fingerprint},
        "candidate": {"name": candidate.name, "fingerprint": candidate.fingerprint},
        "independence": campaign.independence,
        "estimand": "selected_annotation_groups_only",
        "status": "measured" if cells else "unmeasured",
        "quality_measured": bool(cells),
        "excluded_nonindependent_categories": excluded_categories,
        "partitions": dict(partitions),
        "dev_errors": details,
    }
