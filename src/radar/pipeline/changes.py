"""Change log between two project builds. Pure: (previous projects, new projects, now) -> changes.

Unchanged projects keep their previous `updated_at`, so `updated_at` means "last real change".
The first build of an empty database logs every project as new (baseline).
"""

from collections.abc import Callable, Iterable
from datetime import datetime

from radar.domain.enums import ChangeKind
from radar.domain.models import FieldChange, Project, ProjectChange

# Fields a client cares about. Values are rendered as short strings for the log.
TRACKED: dict[str, Callable[[Project], object]] = {
    "name": lambda p: p.name,
    "category": lambda p: p.category.value,
    "developer": lambda p: p.developer,
    "commune": lambda p: p.location.commune,
    "location_precision": lambda p: p.location.precision,
    "power_mw": lambda p: p.power.mw,
    "power_mw_min": lambda p: p.power.mw_min,
    "energy_mwh": lambda p: p.power.mwh,
    "power_status": lambda p: p.power.status.value,
    "substation": lambda p: p.substation,
    "status": lambda p: p.status,
    "confidence": lambda p: p.confidence.value,
    "filings": lambda p: ", ".join(p.filing_keys),
    "signals": lambda p: ", ".join(p.signal_keys),
}


def _render(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def diff_fields(before: Project, after: Project) -> tuple[FieldChange, ...]:
    changes = []
    for name, read in TRACKED.items():
        old, new = _render(read(before)), _render(read(after))
        if old != new:
            changes.append(FieldChange(field=name, before=old, after=new))
    return tuple(changes)


def track_changes(
    previous: Iterable[Project], current: Iterable[Project], now: datetime
) -> tuple[list[Project], list[ProjectChange]]:
    """Return `current` with stable `updated_at` for unchanged projects, and the change log entries."""
    old = {p.id: p for p in previous}
    projects: list[Project] = []
    log: list[ProjectChange] = []
    for p in current:
        before = old.pop(p.id, None)
        fields = diff_fields(before, p) if before else ()
        if before is not None and not fields:
            projects.append(p.model_copy(update={"updated_at": before.updated_at}))
            continue
        projects.append(p.model_copy(update={"updated_at": now}))
        log.append(_entry(p, ChangeKind.CHANGED if before else ChangeKind.NEW, now, fields))
    log.extend(_entry(gone, ChangeKind.REMOVED, now) for gone in old.values())
    return projects, log


def _entry(p: Project, kind: ChangeKind, at: datetime, fields: tuple[FieldChange, ...] = ()) -> ProjectChange:
    return ProjectChange(
        at=at,
        kind=kind,
        project_id=p.id,
        name=p.name,
        category=p.category,
        department=p.location.department,
        fields=fields,
    )
