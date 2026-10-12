"""One evaluation report (JSON) + flat metrics compared with the previous run to flag regressions.

Stored in the `runs` table (kind "eval"), so `radar eval` always answers "better or worse than last time?".
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from radar.classification.gold import GoldLabel
from radar.domain.enums import TARGET_CATEGORIES
from radar.domain.models import Filing
from radar.evaluation.classification import ClassifierReport, screening_report
from radar.evaluation.fields import score_fields
from radar.evaluation.gold import FieldGold

COMPLETENESS_FIELDS: tuple[str, ...] = ("developer", "siret", "commune", "filed_on", "power", "substation", "gps")
LOWER_IS_BETTER = frozenset({"privacy.leaks", "ocr.mean_word_miss_rate"})


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


def build_report(
    filings: list[Filing],
    labels: dict[str, GoldLabel],
    field_gold: dict[str, FieldGold],
    privacy_leaks: list[str],
    ocr: dict[str, Any] | None = None,
    classifier: ClassifierReport | None = None,
) -> dict[str, Any]:
    screening = screening_report(filings, labels)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "gold": {
            "labels": len(labels),
            "labels_verified": sum(label.verified for label in labels.values()),
            "fields": len(field_gold),
            "fields_verified": sum(g.verified for g in field_gold.values()),
        },
        "screening": {
            "recall": screening.recall,
            "precision": round(screening.precision, 3) if screening.precision is not None else None,
            "missed": screening.missed,
            "by_method": screening.by_method,
        },
        "completeness": completeness(filings),
        "fields_verified": {k: s.as_dict() for k, s in score_fields(filings, field_gold, verified=True).items()},
        "fields_draft": {k: s.as_dict() for k, s in score_fields(filings, field_gold, verified=False).items()},
        "privacy": {"leaks": privacy_leaks},
        "ocr": ocr or {"skipped": "no OCR engine available here (Tesseract runs in the Docker image)"},
    }
    if classifier is not None:
        report["classifier"] = {**asdict(classifier), "accuracy": classifier.accuracy}
    return report


def metrics(report: dict[str, Any]) -> dict[str, float]:
    """Flat numeric metrics used for the comparison with the previous run."""
    out: dict[str, float] = {}
    if (recall := report["screening"]["recall"]) is not None:
        out["screening.recall"] = recall
    out.update({f"completeness.{k}": v for k, v in report["completeness"].items()})
    for group in ("fields_verified", "fields_draft"):
        for name, score in report[group].items():
            if score["accuracy"] is not None:
                out[f"{group}.{name}.accuracy"] = score["accuracy"]
    out["privacy.leaks"] = float(len(report["privacy"]["leaks"]))
    if (miss := report["ocr"].get("mean_word_miss_rate")) is not None:
        out["ocr.mean_word_miss_rate"] = miss
    if (accuracy := report.get("classifier", {}).get("accuracy")) is not None:
        out["classifier.accuracy"] = accuracy
    return out


def compare(previous: dict[str, float], current: dict[str, float]) -> dict[str, list[dict[str, Any]]]:
    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for name in sorted(set(previous) & set(current)):
        before, after = previous[name], current[name]
        if abs(after - before) < 1e-9:
            continue
        worse = after > before if name in LOWER_IS_BETTER else after < before
        (regressions if worse else improvements).append({"metric": name, "before": before, "after": after})
    return {"regressions": regressions, "improvements": improvements}
