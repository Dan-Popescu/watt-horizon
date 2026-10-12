"""Drift: a source or a document template suddenly gets worse (site redesign, new form version, OCR change).

Compares filings fetched in the last WINDOW_DAYS with older ones, per source and per template:
- completeness of key fields on tracked projects (should not drop),
- share of filings with a warning/error flag (should not rise).
An alert needs MIN_N filings on both sides and a change of at least DELTA (absolute share).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from radar.domain.enums import TARGET_CATEGORIES, Severity
from radar.domain.fields import machine_values
from radar.domain.models import Filing

WINDOW_DAYS = 30
MIN_N = 5
DELTA = 0.2
TRACKED = ("developer", "commune", "power_mw", "filed_on")


@dataclass
class DriftAlert:
    group: str  # "source" | "template"
    value: str
    metric: str  # "completeness:<field>" | "doubt_rate"
    before: float
    recent: float
    n_before: int
    n_recent: int


def _groups(f: Filing) -> list[tuple[str, str]]:
    out = [("source", f.source.value)]
    out += [("template", t) for t in f.strata.get("template", "").split("+") if t]
    return out


def _share(items: list[bool]) -> float:
    return sum(items) / len(items) if items else 0.0


def drift(filings: Iterable[Filing], first_fetched: dict[str, datetime], now: datetime) -> list[DriftAlert]:
    cutoff = now - timedelta(days=WINDOW_DAYS)
    buckets: dict[tuple[str, str], dict[str, list[Filing]]] = {}
    for f in filings:
        fetched = first_fetched.get(f.key)
        if fetched is None:
            continue
        side = "recent" if fetched >= cutoff else "before"
        for slot in _groups(f):
            buckets.setdefault(slot, {"recent": [], "before": []})[side].append(f)
    alerts = []
    for (group, value), sides in sorted(buckets.items()):
        before, recent = sides["before"], sides["recent"]
        doubt = [
            [any(g.severity in {Severity.WARNING, Severity.ERROR} for g in f.flags) for f in side]
            for side in (before, recent)
        ]
        if len(before) >= MIN_N and len(recent) >= MIN_N and _share(doubt[1]) - _share(doubt[0]) >= DELTA:
            alerts.append(
                DriftAlert(
                    group,
                    value,
                    "doubt_rate",
                    round(_share(doubt[0]), 3),
                    round(_share(doubt[1]), 3),
                    len(before),
                    len(recent),
                )
            )
        targets = [[f for f in side if f.classification.category in TARGET_CATEGORIES] for side in (before, recent)]
        if len(targets[0]) < MIN_N or len(targets[1]) < MIN_N:
            continue
        for name in TRACKED:
            shares = [_share([machine_values(f).get(name) not in (None, "") for f in side]) for side in targets]
            if shares[0] - shares[1] >= DELTA:
                alerts.append(
                    DriftAlert(
                        group,
                        value,
                        f"completeness:{name}",
                        round(shares[0], 3),
                        round(shares[1], 3),
                        len(targets[0]),
                        len(targets[1]),
                    )
                )
    return alerts
