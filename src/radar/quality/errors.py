"""Learn from human corrections: where the machine fails (clusters), whether detectors predicted it (flag
precision / recall), and which past errors are now fixed. Pure functions over filings + corrections.

- cluster = (field, outcome, cause, form layout, how the document was read): the top clusters say which rule,
  layout or OCR path to improve first.
- unflagged errors = corrections no doubt detector anticipated: each one suggests a new detector (quality/flags.py).
- fixed = the machine now gives the value the human typed: the correction can stay (harmless) and becomes a
  regression case (quality/cases.py).
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from radar.domain.enums import Severity
from radar.domain.fields import Value, machine_values
from radar.domain.models import Correction, Filing
from radar.evaluation.fields import compare
from radar.pipeline.corrections import VALIDATED, latest


@dataclass(frozen=True)
class ErrorRecord:
    number: str
    field: str
    outcome: str  # wrong | missed | spurious (machine vs human)
    machine: Value
    human: Value
    cause: str  # reviewer's cause, or "?" when not given
    form_layout: str
    read: str
    flagged: bool  # a warning/error flag was raised on this filing
    blind: bool


@dataclass
class ErrorReport:
    reviewed: int = 0  # filings with at least one human decision
    with_errors: int = 0
    errors: list[ErrorRecord] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)  # "number:field" now matching the human value
    clusters: list[dict[str, object]] = field(default_factory=list)
    flag_precision: float | None = None  # flagged reviewed filings that had an error
    flag_recall: float | None = None  # filings with errors that were flagged
    by_flag: dict[str, dict[str, int]] = field(default_factory=dict)  # code -> {flagged, with_errors}
    unflagged: list[str] = field(default_factory=list)  # errors no detector anticipated: write a detector


def _doubtful(filing: Filing) -> bool:
    return any(f.severity in {Severity.WARNING, Severity.ERROR} for f in filing.flags)


def analyse(filings: Iterable[Filing], corrections: Iterable[Correction]) -> ErrorReport:
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    report = ErrorReport()
    flagged_reviewed = flagged_with_errors = 0
    for filing in filings:
        decisions = latest(by_key.get(filing.key, []))
        if not decisions:
            continue
        report.reviewed += 1
        machine = machine_values(filing)
        errors = []
        for name, c in decisions.items():
            if name == VALIDATED or name not in machine:
                continue
            outcome = compare(c.value, machine[name])
            if outcome in {"correct", "absent"}:
                if c.cause:  # was an error when corrected, the machine has caught up since
                    report.fixed.append(f"{filing.number}:{name}")
                continue
            errors.append(
                ErrorRecord(
                    number=filing.number,
                    field=name,
                    outcome=outcome,
                    machine=machine[name],
                    human=c.value,
                    cause=c.cause or "?",
                    form_layout=filing.strata.get("form_layout", "?"),
                    read=filing.strata.get("read", "?"),
                    flagged=_doubtful(filing),
                    blind=c.blind,
                )
            )
        report.errors += errors
        report.with_errors += bool(errors)
        if _doubtful(filing):
            flagged_reviewed += 1
            flagged_with_errors += bool(errors)
        elif errors:
            report.unflagged += [f"{e.number}:{e.field} ({e.outcome})" for e in errors]
        for code in {f.code for f in filing.flags}:
            stats = report.by_flag.setdefault(code, {"flagged": 0, "with_errors": 0})
            stats["flagged"] += 1
            stats["with_errors"] += bool(errors)

    report.flag_precision = flagged_with_errors / flagged_reviewed if flagged_reviewed else None
    report.flag_recall = flagged_with_errors / report.with_errors if report.with_errors else None
    counts = Counter((e.field, e.outcome, e.cause, e.form_layout, e.read) for e in report.errors)
    report.clusters = [
        {
            "field": f,
            "outcome": o,
            "cause": c,
            "form_layout": layout,
            "read": read,
            "count": n,
            "examples": sorted(
                {
                    e.number
                    for e in report.errors
                    if (e.field, e.outcome, e.cause, e.form_layout, e.read) == (f, o, c, layout, read)
                }
            )[:5],
        }
        for (f, o, c, layout, read), n in counts.most_common()
    ]
    return report
