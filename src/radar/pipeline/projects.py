"""Projects = target filings, enriched with the keys of signals linked to them. Pure function."""

from collections.abc import Iterable
from datetime import datetime

from radar.domain.enums import TARGET_CATEGORIES, Category, ReviewDecision
from radar.domain.ids import project_id
from radar.domain.models import Filing, Link, Project, Signal

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
    filings: Iterable[Filing], signals: Iterable[Signal], links: Iterable[Link], now: datetime
) -> list[Project]:
    signal_keys = {s.key for s in signals}
    linked: dict[str, set[str]] = {}
    for link in links:
        for this, other in ((link.a, link.b), (link.b, link.a)):
            if other in signal_keys:
                linked.setdefault(this, set()).add(other)

    projects = []
    for f in filings:
        category = f.classification.category
        if category not in TARGET_CATEGORIES:
            continue
        place = f.location.commune or "commune inconnue"
        projects.append(
            Project(
                id=project_id(f.key),
                name=f"{SHORT_LABELS[category]} — {place}",
                category=category,
                developer=f.developer,
                location=f.location,
                power=f.power,
                substation=f.substation,
                status=STATUS_LABELS[f.review_decision],
                first_seen=f.filed_on,
                updated_at=now,
                confidence=f.confidence,
                filing_keys=(f.key,),
                signal_keys=tuple(sorted(linked.get(f.key, ()))),
            )
        )
    return projects
