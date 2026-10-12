"""Collector health from the `runs` history: catch silent breakage (a site changed its layout, blocked us, or
the scheduled job stopped). Pure function over run records; `radar health` prints it.

Flags per source (latest run first):
- blocked   the last run stopped on purpose (the site refused us): wait, never retry in a loop
- errors    the last run recorded errors
- empty     the last EMPTY_STREAK finished runs saw nothing (layout change? wrong URL?)
- stale     no finished run for STALE_DAYS (scheduler down?)
- crashed   the last run never finished
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

STALE_DAYS = 3
EMPTY_STREAK = 3
ALERTS = frozenset({"blocked", "crashed", "empty"})


@dataclass
class SourceHealth:
    source: str
    status: str = "ok"  # ok | warning | alert
    flags: list[str] = field(default_factory=list)
    last_finished: str | None = None
    last_seen: int | None = None
    last_kept: int | None = None
    detail: list[str] = field(default_factory=list)


def collector_health(runs: Iterable[dict[str, Any]], now: datetime) -> list[SourceHealth]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for run in runs:  # most recent first
        if run["kind"].startswith("collect:"):
            by_source.setdefault(run["kind"].removeprefix("collect:"), []).append(run)
    report = []
    for source, history in sorted(by_source.items()):
        health = SourceHealth(source)
        last = history[0]
        finished = [r for r in history if r["finished_at"] is not None]
        if last["finished_at"] is None:
            health.flags.append("crashed")
        if finished:
            stats = finished[0]["stats"]
            health.last_finished = finished[0]["finished_at"].isoformat(timespec="minutes")
            health.last_seen, health.last_kept = stats.get("seen"), stats.get("kept")
            if stats.get("stopped"):
                health.flags.append("blocked")
                health.detail.append(str(stats["stopped"]))
            if stats.get("errors"):
                health.flags.append("errors")
                health.detail += [str(e) for e in stats["errors"][:3]]
            streak = finished[:EMPTY_STREAK]
            if len(streak) == EMPTY_STREAK and all(
                not r["stats"].get("seen") and not r["stats"].get("stopped") for r in streak
            ):
                health.flags.append("empty")
            if now - finished[0]["finished_at"] > timedelta(days=STALE_DAYS):
                health.flags.append("stale")
        else:
            health.flags.append("stale")
        health.status = "alert" if ALERTS & set(health.flags) else ("warning" if health.flags else "ok")
        report.append(health)
    return report
