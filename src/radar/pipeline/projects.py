"""Projects = groups of target filings (same physical project), enriched with linked signals. Pure function."""

from collections.abc import Iterable, Mapping
from datetime import date, datetime

from radar.domain.enums import TARGET_CATEGORIES, Category, PowerStatus, ReviewDecision
from radar.domain.ids import project_id
from radar.domain.models import Filing, Link, Project, Signal
from radar.linking.resolve import group_filings

SHORT_LABELS: dict[Category, str] = {
    Category.BESS: "Batteries",
    Category.BESS_HYBRID: "Batteries (hybride)",
    Category.STORAGE_SUBSTATION: "Poste pour batteries",
    Category.DATACENTER: "Data center",
}
STATUS_LABELS: dict[ReviewDecision, str] = {
    ReviewDecision.NOT_SUBJECT: "Cas par cas : non soumis à étude d'impact",
    ReviewDecision.SUBJECT: "Cas par cas : soumis à étude d'impact (enquête publique à venir)",
    ReviewDecision.PENDING: "Cas par cas : demande déposée",
}


def build_projects(
    filings: Iterable[Filing],
    signals: Iterable[Signal],
    links: Iterable[Link],
    now: datetime,
    commissioned: Mapping[str, Signal] | None = None,
) -> list[Project]:
    """One project per group of target filings (see linking.resolve); the most recent filing leads.
    Human link verdicts win: « same_project » merges, « related » / « unrelated » keep apart, and an « unrelated »
    signal is not attached."""
    commissioned = commissioned or {}
    signal_keys = {s.key for s in signals}
    links = list(links)
    must = [(lk.a, lk.b) for lk in links if lk.verdict == "same_project"]
    cannot = [(lk.a, lk.b) for lk in links if lk.verdict in {"related", "unrelated"}]
    linked: dict[str, set[str]] = {}
    for link in links:
        if link.verdict == "unrelated":
            continue
        for this, other in ((link.a, link.b), (link.b, link.a)):
            if other in signal_keys:
                linked.setdefault(this, set()).add(other)

    projects = []
    targets = (f for f in filings if f.classification.category in TARGET_CATEGORIES)
    for group in group_filings(targets, must, cannot):
        newest = sorted(group, key=lambda f: (f.filed_on or date.min, f.key), reverse=True)
        lead = newest[0]
        category = lead.classification.category
        location = next((f.location for f in newest if f.location.precision == "exact"), lead.location)
        power = next((f.power for f in newest if f.power.status is PowerStatus.KNOWN), None) or next(
            (f.power for f in newest if f.power.mw is not None), lead.power
        )
        installation = next((commissioned[f.key] for f in newest if f.key in commissioned), None)
        status = STATUS_LABELS[lead.review_decision]
        if installation is not None:
            day = installation.observed_on.strftime("%d/%m/%Y") if installation.observed_on else "date inconnue"
            status = f"En service (registre national, mise en service le {day})"
        dates = [f.filed_on for f in group if f.filed_on]
        signals_of_group = set().union(*(linked.get(f.key, set()) for f in group))
        if installation is not None:
            signals_of_group.add(installation.key)
        projects.append(
            Project(
                id=project_id(min(f.key for f in group)),
                name=f"{SHORT_LABELS[category]} — {location.commune or 'commune inconnue'}",
                category=category,
                developer=lead.developer,
                location=location,
                power=power,
                substation=next((f.substation for f in newest if f.substation), None),
                status=status,
                first_seen=min(dates) if dates else None,
                updated_at=now,
                confidence=lead.confidence,
                filing_keys=(lead.key, *sorted(f.key for f in group if f is not lead)),
                signal_keys=tuple(sorted(signals_of_group)),
            )
        )
    return projects
