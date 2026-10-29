"""Explicit, atomic evaluation approvals tied to a logical dataset snapshot."""

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.evaluation.gold import FIELDS
from radar.evaluation.partitions import atomic_json
from radar.evaluation.report import compare, metrics

REQUIRED_FIELDS = ("developer", "commune", "power_mw", "filed_on", "review_decision")


def _promotion_metrics(report: dict[str, Any]) -> dict[str, float]:
    # Training scores, completeness and automation coverage are diagnostics, not evidence of quality.
    diagnostic = (
        "fields_verified.dev.",
        "fields_verified.calibration.",
        "fields_draft.",
        "completeness.",
        "auto_accept.",
        "second_opinion.",
    )
    output = {key: value for key, value in metrics(report).items() if not key.startswith(diagnostic)}
    for name, score in report.get("fields_verified", {}).get("test", {}).items():
        key = f"fields_verified.test.{name}.accuracy"
        if (accuracy := score.get("independent_accuracy")) is None:
            output.pop(key, None)
        else:
            output[key] = accuracy
    return output


def dataset_fingerprint(path: Path) -> str:
    """Hash durable dataset rows, excluding run history and derived caches.

    A read transaction gives a coherent snapshot even if a writer commits concurrently. A SQLite backup
    of the same dataset produces the same identity despite different pages, journal or timestamps.
    """
    if not path.is_file():
        raise ValueError("Dataset database does not exist")
    digest = hashlib.sha256()
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        relevant = {
            "documents",
            "document_attachments",
            "filings",
            "signals",
            "links",
            "projects",
            "corrections",
            "link_decisions",
            "listing_items",
            "machine_changes",
        }
        tables = sorted(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] in relevant
        )
        for table in tables:
            digest.update(table.encode())
            # Names are selected only from the fixed set above, not from input.
            cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            columns = [column[0] for column in cursor.description]
            digest.update(json.dumps(columns, separators=(",", ":")).encode())
            for row in cursor:
                digest.update(json.dumps(tuple(row), sort_keys=True, separators=(",", ":"), default=str).encode())
        connection.rollback()
    finally:
        connection.close()
    return digest.hexdigest()


def assess_report(
    report: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    min_samples: int = 30,
    required_fields: tuple[str, ...] = REQUIRED_FIELDS,
    min_accuracy: float = 0.95,
    min_screening_recall: float = 0.95,
) -> dict[str, Any]:
    """Fail closed on unavailable mandatory measurements, changed populations and regressions."""
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    if (
        not required_fields
        or len(set(required_fields)) != len(required_fields)
        or not set(required_fields) <= set(FIELDS)
    ):
        raise ValueError("required_fields must contain distinct field names")
    if not 0 < min_accuracy <= 1 or not 0 < min_screening_recall <= 1:
        raise ValueError("Accuracy and screening recall thresholds must be in (0, 1]")
    profile = {
        "min_samples": min_samples,
        "required_fields": list(required_fields),
        "min_accuracy": min_accuracy,
        "min_screening_recall": min_screening_recall,
    }
    failures: list[str] = []
    previous_report = previous.get("report", previous) if previous else None
    try:
        current = _promotion_metrics(report)
    except (KeyError, TypeError, ValueError):
        current = {}
        failures.append("invalid_report")
    test_scores = report.get("fields_verified", {}).get("test", {})
    if not test_scores or any(score.get("independent_n", 0) < min_samples for score in test_scores.values()):
        failures.append("independent_field_samples_missing")
    for name in required_fields:
        score = test_scores.get(name)
        if score is None:
            failures.append(f"required_field_missing:{name}")
            continue
        value = score.get("independent_accuracy")
        if not isinstance(value, int | float) or not math.isfinite(value) or value < min_accuracy:
            failures.append(f"field_accuracy_below_threshold:{name}")
    if (recall := current.get("screening.recall")) is not None and recall < min_screening_recall:
        failures.append("screening_recall_below_threshold")
    if report.get("screening", {}).get("test_independent_n", 0) < min_samples:
        failures.append("verified_classification_samples_missing")
    if report.get("screening", {}).get("test_target_independent_n", 0) < min_samples:
        failures.append("independent_screening_targets_missing")
    if "classifier" in report and report["classifier"].get("test_independent_n", 0) < min_samples:
        failures.append("independent_classifier_samples_missing")
    if not report.get("partitions", {}).get("fingerprint"):
        failures.append("partition_manifest_missing")
    if not report.get("reference_fingerprint"):
        failures.append("reference_identity_missing")
    for metric in ("screening.recall", "privacy.leaks", "integrity.quote_not_found"):
        if metric not in current or not math.isfinite(current[metric]):
            failures.append(f"required_metric_unavailable:{metric}")
    if current.get("privacy.leaks", 1) != 0:
        failures.append("privacy_leaks")
    if current.get("integrity.quote_not_found", 1) != 0:
        failures.append("invalid_evidence")
    for key in ("failed", "reconstruction_failures"):
        if report.get(key) or report.get("validation", {}).get(key) or report.get("robustness", {}).get(key):
            failures.append(f"execution_{key}")
    if report.get("ocr", {}).get("failed_documents"):
        failures.append("ocr_execution_failed")
    if report.get("validation", {}).get("missing_reference_filings_count", 0):
        failures.append("reference_filings_missing")
    if report.get("cases", {}).get("regressed", 0):
        failures.append("regression_cases_failed")
    comparison: dict[str, list[dict[str, Any]]] = {"regressions": [], "improvements": []}
    if previous_report:
        if previous and previous.get("profile", profile) != profile:
            failures.append("approval_profile_changed")
        comparison = compare(_promotion_metrics(previous_report), current)
        if comparison["regressions"]:
            failures.append("metrics_regressed")
        for key, reason in (
            ("partitions", "evaluation_population_changed"),
            ("reference_fingerprint", "reference_population_changed"),
        ):
            before = previous_report.get(key)
            now = report.get(key)
            if key == "partitions":
                before = (before or {}).get("evaluation_fingerprint", (before or {}).get("fingerprint"))
                now = (now or {}).get("evaluation_fingerprint", (now or {}).get("fingerprint"))
            if before != now:
                failures.append(reason)
        for name, old in previous_report.get("fields_verified", {}).get("test", {}).items():
            if name not in test_scores or any(
                test_scores[name].get(unit, 0) != old.get(unit, 0) for unit in ("n", "independent_n")
            ):
                failures.append(f"sample_population_changed:{name}")
    return {
        "status": "failed" if failures else "passed",
        "failures": sorted(set(failures)),
        "comparison": comparison,
        "min_samples": min_samples,
        "profile": profile,
    }


def load_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("report"), dict):
        raise ValueError("Invalid approved evaluation baseline")
    return value


def approve_baseline(
    path: Path,
    report: dict[str, Any],
    *,
    dataset_id: str,
    min_samples: int = 30,
    required_fields: tuple[str, ...] = REQUIRED_FIELDS,
    min_accuracy: float = 0.95,
    min_screening_recall: float = 0.95,
    new_campaign: bool = False,
) -> dict[str, Any]:
    if not dataset_id:
        raise ValueError("Dataset identity is required for approval")
    previous = load_baseline(path)
    gate = assess_report(
        report,
        previous=None if new_campaign else previous,
        min_samples=min_samples,
        required_fields=required_fields,
        min_accuracy=min_accuracy,
        min_screening_recall=min_screening_recall,
    )
    if gate["status"] != "passed":
        raise ValueError("Evaluation approval failed: " + ", ".join(gate["failures"]))
    value = {
        "schema_version": 1,
        "dataset_fingerprint": dataset_id,
        "report": report,
        "metrics": _promotion_metrics(report),
        "gate": gate,
        "profile": gate["profile"],
        "approved_at": datetime.now(UTC).isoformat(),
    }
    if previous:
        identity = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
        atomic_json(path.parent / "history" / f"{identity}.json", previous)
    atomic_json(path, value)
    return value


def approved_for_dataset(path: Path, fingerprint: str) -> dict[str, Any]:
    value = load_baseline(path)
    if value is None:
        raise ValueError("Dataset has no approved evaluation baseline")
    if value.get("dataset_fingerprint") != fingerprint:
        raise ValueError("Approved baseline belongs to a different dataset")
    profile = value.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("Dataset evaluation approval has no explicit quality profile")
    gate = assess_report(
        value["report"],
        min_samples=profile["min_samples"],
        required_fields=tuple(profile["required_fields"]),
        min_accuracy=profile["min_accuracy"],
        min_screening_recall=profile["min_screening_recall"],
    )
    if value.get("gate", {}).get("status") != "passed" or gate["status"] != "passed":
        raise ValueError("Dataset evaluation approval is invalid")
    return value
