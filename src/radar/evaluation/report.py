"""One evaluation report (JSON) + flat metrics compared with the previous run to flag regressions.

Stored in the `runs` table (kind "eval"), so `radar eval` always answers "better or worse than last time?".
Every rate carries its sample size and a 95 % interval; the test split shows scores only (see evaluation/stats).
"""

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from radar.classification import bayes
from radar.classification.gold import GoldLabel
from radar.domain.enums import TARGET_CATEGORIES
from radar.domain.models import Correction, Filing, Link, LinkDecision
from radar.domain.references import canonical_reference_map
from radar.evaluation import rigor
from radar.evaluation.classification import ClassifierReport, screening_report, stored_classification_report
from radar.evaluation.fields import score_fields
from radar.evaluation.gold import FieldGold
from radar.evaluation.partitions import PartitionManifest, with_partitions
from radar.evaluation.stats import partition_of, split_of
from radar.linking import review as link_review
from radar.quality import agreement, model_check
from radar.quality.arbiter import learn
from radar.quality.confidence import calibrate
from radar.quality.errors import analyse

LOWER_IS_BETTER = frozenset(
    {
        "privacy.leaks",
        "ocr.mean_word_miss_rate",
        "integrity.quote_not_found",
        "cases.regressed",
        "undecided.share",
        "calibration.ece",
        "ocr.mean_measurement_miss_rate",
        "ocr.mean_word_error_rate",
        "ocr.mean_character_error_rate",
        "ocr.unread_scanned_pages",
    }
)
TEST_SPLIT_CAVEAT = (
    "Les dossiers traités avant le 2026-10-12 ont servi à régler les règles, y compris ceux du jeu de test : "
    "le score « test » n'est fiable que sur les dossiers ajoutés depuis."
)


@dataclass
class ReportInputs:
    filings: list[Filing]
    labels: dict[str, GoldLabel]
    field_gold: dict[str, FieldGold]
    corrections: list[Correction] = field(default_factory=list)
    privacy_leaks: list[str] = field(default_factory=list)
    listing: dict[str, int] = field(default_factory=dict)
    cases: dict[str, list[str]] | None = None
    ocr: dict[str, Any] | None = None
    classifier: ClassifierReport | None = None
    links: Iterable[Link] = field(default_factory=tuple)  # consumed once; never retain a dense graph here
    link_decisions: dict[tuple[str, str], LinkDecision] = field(default_factory=dict)
    partition_manifest: PartitionManifest | None = None
    failures: dict[str, str] = field(default_factory=dict)


def completeness(filings: list[Filing]) -> dict[str, float]:
    """Share of target filings with a value (not correctness: see the field scores)."""
    targets = [f for f in filings if f.classification.category in TARGET_CATEGORIES]
    if not targets:
        return {}
    values: dict[str, Callable[[Filing], object]] = {
        "developer": lambda f: f.developer,
        "siret": lambda f: f.siret,
        "commune": lambda f: f.location.commune,
        "filed_on": lambda f: f.filed_on,
        "power": lambda f: f.power.mw,
        "substation": lambda f: f.substation,
        "gps": lambda f: f.location.precision == "exact" or None,
    }
    return {
        name: round(sum(read(f) not in (None, "") for f in targets) / len(targets), 3) for name, read in values.items()
    }


def _fields_by_split(filings: list[Filing], gold: dict[str, FieldGold]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    assigned = {f.key: partition_of(f) for f in filings}
    for split in ("dev", "calibration", "test"):
        part = {n: g for n, g in gold.items() if assigned.get(n, split_of(n)) == split}
        scores = score_fields(filings, part, verified=True)
        out[split] = {k: s.as_dict(with_errors=split == "dev") for k, s in scores.items()}
    return out


def _field_calibration(filings: list[Filing], corrections: list[Correction]) -> dict[str, Any]:
    from radar.quality.confidence import field_observations

    calibration = calibrate(filings, corrections)
    targets = [f for f in filings if f.classification.category in TARGET_CATEGORIES]
    scored = sum(len(f.field_score) for f in targets)
    accepted = sum(len(f.auto_accepted) for f in targets)
    return {
        "diagram": calibration.diagram(),
        "ece": calibration.field_ece(field_observations(filings, corrections, partition="test")),
        "auto_accept_share": round(accepted / scored, 3) if scored else None,
    }


def _draft_fields(filings: list[Filing], gold: dict[str, FieldGold]) -> dict[str, Any]:
    visible = {f.key for f in filings if partition_of(f) == "dev"}
    details = score_fields(filings, {n: g for n, g in gold.items() if n in visible}, verified=False)
    return {
        name: score.as_dict(with_errors=False) | {"errors": details[name].errors if name in details else []}
        for name, score in score_fields(filings, gold, verified=False).items()
    }


def build_report(inputs: ReportInputs) -> dict[str, Any]:
    filings = (
        with_partitions(inputs.filings, inputs.partition_manifest) if inputs.partition_manifest else inputs.filings
    )
    labels = canonical_reference_map(filings, inputs.labels)
    field_gold = canonical_reference_map(filings, inputs.field_gold)
    known_keys = {f.key for f in filings}
    dev_keys = {f.key for f in filings if partition_of(f) == "dev"}
    screening = screening_report(filings, {n: label for n, label in labels.items() if label.verified})
    test_numbers = {f.key for f in filings if partition_of(f) == "test"}
    independent_screening = screening_report(
        filings, {n: label for n, label in labels.items() if label.verified and n in test_numbers}
    )
    errors = analyse(filings, inputs.corrections)
    with_errors, reviewed = rigor.reviewed_errors(filings, inputs.corrections)
    cases: dict[str, Any] = (
        {k: len(v) for k, v in inputs.cases.items()} | {"regressed_cases": inputs.cases["regressed"]}
        if inputs.cases is not None
        else {"skipped": "no gold/cases yet"}
    )
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gold": {
            "labels": len(labels),
            "labels_verified": sum(label.verified for label in labels.values()),
            "fields": len(field_gold),
            "fields_verified": sum(g.verified for g in field_gold.values()),
            "reviewed_filings": len(reviewed),
        },
        "screening": {
            "recall_on_gold": screening.recall,
            "test_recall": independent_screening.recall,
            "test_targets": independent_screening.gold_targets,
            "test_independent_n": independent_screening.independent_n,
            "test_target_independent_n": independent_screening.independent_target_n,
            "test_group_recall": independent_screening.independent_recall,
            "precision": round(screening.precision, 3) if screening.precision is not None else None,
            "missed": [n for n in screening.missed if n in dev_keys],
            "by_method": screening.by_method,
            "recall_estimate": rigor.recall_estimate(filings, inputs.corrections, inputs.listing),
        },
        "completeness": completeness(filings),
        "stored_classification": stored_classification_report(filings, labels),
        "fields_verified": _fields_by_split(filings, field_gold),
        "fields_draft": _draft_fields(filings, field_gold),
        "by_stratum": rigor.by_stratum(filings, field_gold),
        "calibration": rigor.calibration(filings, with_errors, reviewed),
        "blind": rigor.blind_accuracy(filings, inputs.corrections),
        "flags": {
            "precision": errors.flag_precision,
            "recall": errors.flag_recall,
            "unflagged_errors": errors.unflagged,
            "by_flag": errors.by_flag,
        },
        "second_opinion": bayes.summary(
            bayes.train(bayes.trusted_examples(filings, {n: g.category for n, g in labels.items() if g.verified}))
        ),
        "linking": asdict(link_review.report(filings, inputs.links, inputs.link_decisions)),
        "agreement": asdict(
            agreement.agreement(filings, inputs.corrections, {f.number for f in filings if partition_of(f) == "test"})
        ),
        "model_check": model_check.reliability(filings, inputs.corrections).as_dict(),
        "arbitration": learn(filings, inputs.corrections).table(),
        "field_calibration": _field_calibration(filings, inputs.corrections),
        "integrity": {"quote_not_found": sum(g.code == "quote_not_found" for f in filings for g in f.flags)},
        "undecided": {"share": rigor.undecided_share(filings)},
        "cases": cases,
        "privacy": {"leaks": inputs.privacy_leaks},
        "ocr": inputs.ocr or {"skipped": "no OCR engine available here (Tesseract runs in the Docker image)"},
        "caveats": [TEST_SPLIT_CAVEAT],
        "validation": {
            "reconstruction_failures": inputs.failures,
            "missing_reference_filings_count": len(
                (
                    {key for key, label in labels.items() if label.verified}
                    | {key for key, reference in field_gold.items() if reference.verified}
                )
                - known_keys
            ),
        },
        "partitions": {
            "fingerprint": inputs.partition_manifest.fingerprint if inputs.partition_manifest else None,
            "version": inputs.partition_manifest.version if inputs.partition_manifest else None,
            "evaluation_fingerprint": hashlib.sha256(
                json.dumps(
                    sorted(
                        (f.key, f.strata.get("evaluation_group", f.key)) for f in filings if partition_of(f) == "test"
                    )
                ).encode()
            ).hexdigest()
            if inputs.partition_manifest
            else None,
            "counts": {part: sum(partition_of(f) == part for f in filings) for part in ("dev", "calibration", "test")},
        },
        "reference_fingerprint": hashlib.sha256(
            json.dumps(
                {
                    "labels": sorted(
                        (n, label.category.value) for n, label in labels.items() if label.verified and n in test_numbers
                    ),
                    "fields": sorted(
                        (n, g.expected) for n, g in field_gold.items() if g.verified and n in test_numbers
                    ),
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest(),
    }
    if inputs.classifier is not None:
        report["classifier"] = {
            **asdict(inputs.classifier),
            "accuracy": inputs.classifier.accuracy,
            "test_independent_accuracy": inputs.classifier.test_independent_accuracy,
        }
    return report


def metrics(report: dict[str, Any]) -> dict[str, float]:
    """Flat numeric metrics used for the comparison with the previous run."""
    out: dict[str, float] = {}
    if (accuracy := report.get("stored_classification", {}).get("independent_accuracy")) is not None:
        out["stored_classification.accuracy"] = accuracy
    if (
        recall := report["screening"].get(
            "test_group_recall", report["screening"].get("test_recall", report["screening"]["recall_on_gold"])
        )
    ) is not None:
        out["screening.recall"] = recall
    out.update({f"completeness.{k}": v for k, v in report["completeness"].items()})
    for split, scores in report["fields_verified"].items():
        for k, s in scores.items():
            if s["accuracy"] is not None:
                out[f"fields_verified.{split}.{k}.accuracy"] = s["accuracy"]
    for k, s in report["fields_draft"].items():
        if s["accuracy"] is not None:
            out[f"fields_draft.{k}.accuracy"] = s["accuracy"]
    for k, s in report["blind"].items():
        if s["rate"] is not None:
            out[f"blind.{k}.accuracy"] = s["rate"]
    if (ece := report["field_calibration"]["ece"]) is not None:
        out["calibration.ece"] = ece
    if (share := report["field_calibration"]["auto_accept_share"]) is not None:
        out["auto_accept.share"] = share
    if (stable := report.get("robustness", {}).get("stable_share")) is not None:
        out["robustness.stable_share"] = stable
    out["privacy.leaks"] = float(len(report["privacy"]["leaks"]))
    out["integrity.quote_not_found"] = float(report["integrity"]["quote_not_found"])
    if (share := report["undecided"]["share"]) is not None:
        out["undecided.share"] = share
    if "regressed" in report["cases"]:
        out["cases.regressed"] = float(report["cases"]["regressed"])
        out["cases.fixed"] = float(report["cases"]["fixed"])
    if (miss := report["ocr"].get("mean_word_miss_rate")) is not None:
        out["ocr.mean_word_miss_rate"] = miss
    for name in (
        "mean_measurement_miss_rate",
        "mean_word_error_rate",
        "mean_character_error_rate",
        "unread_scanned_pages",
    ):
        if (value := report["ocr"].get(name)) is not None:
            out[f"ocr.{name}"] = float(value)
    for name in ("precision", "recall"):
        if (value := report["linking"]["grouping"].get(name)) is not None:
            out[f"grouping.{name}"] = value
    # Agreement between reviewers is not compared: it measures the references, not the code.
    if (loo := report["second_opinion"].get("loo_accuracy")) is not None:
        out["second_opinion.loo_accuracy"] = loo
    classifier = report.get("classifier", {})
    if (accuracy := classifier.get("test_independent_accuracy", classifier.get("accuracy"))) is not None:
        out["classifier.accuracy"] = accuracy
    return out


def compare(previous: dict[str, float], current: dict[str, float]) -> dict[str, list[dict[str, Any]]]:
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for name in sorted(previous):
        if name not in current:
            regressions.append({"metric": name, "before": previous[name], "after": None, "reason": "missing_metric"})
            continue
        before, after = previous[name], current[name]
        if abs(after - before) < 1e-9:
            continue
        worse = after > before if name in LOWER_IS_BETTER else after < before
        (regressions if worse else improvements).append({"metric": name, "before": before, "after": after})
    return {"regressions": regressions, "improvements": improvements}
