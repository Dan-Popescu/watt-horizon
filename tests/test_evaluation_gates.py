"""Independent evaluation populations and explicit approval gates."""

from datetime import date
from pathlib import Path

import pytest

from radar.domain.enums import SourceKind
from radar.domain.models import Filing
from radar.evaluation.baseline import approve_baseline, approved_for_dataset, assess_report, dataset_fingerprint
from radar.evaluation.partitions import build_manifest, load_manifest, save_manifest, with_partitions
from radar.evaluation.report import compare
from radar.evaluation.stats import partition_of
from radar.store.db import Database

SMALL_PROFILE = {"min_samples": 1, "required_fields": ("power_mw",)}


def filing(number: str, *documents: str) -> Filing:
    return Filing(
        key=f"d:h:{number}",
        source=SourceKind.DREAL_CASE_BY_CASE,
        region="h",
        number=number,
        title="Synthetic evaluation filing",
        document_ids=tuple(documents),
    )


def test_shared_documents_cannot_cross_partitions(tmp_path: Path) -> None:
    a, b = filing("first", "shared"), filing("second", "shared")
    manifest = build_manifest([a, b])
    prepared = with_partitions([a, b], manifest)
    assert partition_of(prepared[0]) == partition_of(prepared[1])
    path = tmp_path / "partitions.json"
    save_manifest(path, manifest)
    assert load_manifest(path) == manifest
    assert build_manifest([b, a], previous=manifest) == manifest


def test_temporal_holdout_is_frozen_and_group_merges_fail_closed() -> None:
    a, b = filing("old", "a"), filing("new", "b")
    initial = build_manifest(
        [a, b], first_seen={a.key: date(2026, 1, 1), b.key: date(2026, 11, 1)}, test_after=date(2026, 10, 29)
    )
    assert initial.assignments[b.key] == "test"
    # Pick an explicit dev allocation to reproduce a later cross-partition shared document.
    initial.assignments[a.key] = "dev"
    changed = b.model_copy(update={"document_ids": ("a",)})
    with pytest.raises(ValueError, match="partition"):
        build_manifest([a, changed], previous=initial)


def test_missing_required_metric_is_a_regression() -> None:
    result = compare({"fields_verified.test.power_mw.accuracy": 0.99}, {})
    assert result["regressions"] == [
        {
            "metric": "fields_verified.test.power_mw.accuracy",
            "before": 0.99,
            "after": None,
            "reason": "missing_metric",
        }
    ]


def candidate_report() -> dict[str, object]:
    return {
        "gold": {"fields_verified": 1, "labels_verified": 1},
        "screening": {"recall_on_gold": 1.0},
        "completeness": {},
        "fields_verified": {"dev": {}, "calibration": {}, "test": {"power_mw": {"n": 10, "accuracy": 1.0}}},
        "fields_draft": {},
        "blind": {},
        "field_calibration": {"ece": None, "auto_accept_share": None},
        "privacy": {"leaks": []},
        "integrity": {"quote_not_found": 0},
        "undecided": {"share": 0.0},
        "cases": {},
        "ocr": {},
        "linking": {"grouping": {}},
        "second_opinion": {},
        "partitions": {"fingerprint": "frozen-population"},
        "reference_fingerprint": "frozen-gold",
        "robustness": {"stable_share": 1.0},
    }


def test_approval_is_explicit_and_bound_to_data(tmp_path: Path) -> None:
    path = tmp_path / "approved.json"
    report = candidate_report()
    assert assess_report(report, **SMALL_PROFILE)["status"] == "passed"
    approve_baseline(path, report, dataset_id="source-snapshot-a", **SMALL_PROFILE)
    assert approved_for_dataset(path, "source-snapshot-a")["gate"]["status"] == "passed"
    with pytest.raises(ValueError, match="dataset"):
        approved_for_dataset(path, "different-data")
    broken = candidate_report()
    broken["privacy"] = {"leaks": ["synthetic-leak"]}
    with pytest.raises(ValueError, match="approval"):
        approve_baseline(path, broken, dataset_id="source-snapshot-b", **SMALL_PROFILE)
    assert approved_for_dataset(path, "source-snapshot-a")["dataset_fingerprint"] == "source-snapshot-a"


def test_empty_independent_population_cannot_be_approved() -> None:
    report = candidate_report()
    report["fields_verified"] = {"dev": {}, "test": {}}
    gate = assess_report(report)
    assert gate["status"] == "failed" and "independent_field_samples_missing" in gate["failures"]


def test_missing_metric_and_changed_references_block_promotion() -> None:
    old = candidate_report()
    new = candidate_report()
    new["screening"] = {"recall_on_gold": None}
    assert assess_report(new, previous=old)["status"] == "failed"
    new = candidate_report()
    new["reference_fingerprint"] = "changed-gold"
    assert "reference_population_changed" in assess_report(new, previous=old)["failures"]


def test_dataset_identity_ignores_evaluation_run_history(db: Database) -> None:
    before = dataset_fingerprint(db.path)
    run = db.start_run("eval")
    db.finish_run(run, {"synthetic": True})
    assert dataset_fingerprint(db.path) == before


def test_publication_profile_requires_critical_fields_and_actual_quality() -> None:
    report = candidate_report()
    report["gold"] = {"labels_verified": 40, "fields_verified": 40}
    fields = {
        name: {"n": 40, "accuracy": 1.0} for name in ("developer", "commune", "power_mw", "filed_on", "review_decision")
    }
    report["fields_verified"] = {"test": fields}
    assert assess_report(report)["status"] == "passed"
    fields["power_mw"]["accuracy"] = 0.01
    assert "field_accuracy_below_threshold:power_mw" in assess_report(report)["failures"]
    fields["power_mw"]["accuracy"] = 1.0
    del fields["developer"]
    assert "required_field_missing:developer" in assess_report(report)["failures"]
    report["screening"] = {"recall_on_gold": 0.01}
    assert "screening_recall_below_threshold" in assess_report(report)["failures"]
