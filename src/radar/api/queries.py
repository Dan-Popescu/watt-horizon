"""Read-only queries over the database. Pure functions of (Database, arguments) -> view models."""

from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime

from radar.api.views import (
    ChangeView,
    CheckView,
    DatasetInfo,
    DocumentView,
    EvidenceView,
    FieldChangeView,
    ProjectDetail,
    ProjectSummary,
    RelatedView,
    ZoneSummary,
)
from radar.domain.enums import TARGET_CATEGORIES, Category, ChangeKind
from radar.domain.geo import haversine_km
from radar.domain.models import Project, ProjectChange
from radar.store.db import Database

MAX_LIMIT = 200
COVERAGE = "Prototype : Hauts-de-France (DREAL cas par cas 2025-2026 + échantillon 2024), signaux registre et presse."
CAVEATS = [
    "Les positions 'commune' sont le centre de la commune, pas l'emplacement exact.",
    "Les puissances 'estimated' sont des fourchettes calculées, pas des valeurs déclarées.",
    "Les liens 'related' sont des propositions non confirmées.",
    "Confiance 'review' : informations pas encore vérifiées par un humain.",
]


def _summary(p: Project, distance_km: float | None = None) -> ProjectSummary:
    return ProjectSummary(
        project_id=p.id,
        name=p.name,
        category=p.category.value,
        developer=p.developer,
        commune=p.location.commune,
        department=p.location.department,
        lat=p.location.lat,
        lon=p.location.lon,
        location_precision=p.location.precision,
        power_mw=p.power.mw,
        power_mw_min=p.power.mw_min,
        energy_mwh=p.power.mwh,
        power_status=p.power.status.value,
        substation=p.substation,
        status=p.status,
        first_seen=p.first_seen.isoformat() if p.first_seen else None,
        confidence=p.confidence.value,
        distance_km=round(distance_km, 1) if distance_km is not None else None,
    )


def _categories(category: str | None) -> Iterable[str]:
    if category is None:
        return [c.value for c in TARGET_CATEGORIES]
    return [Category(category).value]  # raises ValueError on unknown categories


def search_projects(
    db: Database,
    *,
    category: str | None = None,
    department: str | None = None,
    commune: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 20.0,
    min_mw: float | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[ProjectSummary]:
    near = (lat, lon, radius_km) if lat is not None and lon is not None else None
    rows = db.projects(categories=_categories(category), department=department, near=near, min_mw=min_mw, since=since)
    out: list[ProjectSummary] = []
    for p in rows:
        if commune and (p.location.commune or "").lower() != commune.lower():
            continue
        distance = None
        if near and p.location.lat is not None and p.location.lon is not None:
            distance = haversine_km(near[0], near[1], p.location.lat, p.location.lon)
            if distance > radius_km:
                continue
        out.append(_summary(p, distance))
    if near:
        out.sort(key=lambda s: s.distance_km if s.distance_km is not None else 1e9)
    return out[: max(1, min(limit, MAX_LIMIT))]


def list_projects(db: Database, *, category: str | None = None, department: str | None = None) -> list[ProjectSummary]:
    """Every matching project, uncapped (exports)."""
    return [_summary(p) for p in db.projects(categories=_categories(category), department=department)]


def get_project(db: Database, project_id: str) -> ProjectDetail | None:
    project = db.project(project_id)
    if project is None:
        return None
    filing = db.filing(project.filing_keys[0])
    if filing is None:
        return None
    docs = {d.id: d for d in db.documents(filing.key)}
    signals = {s.key: s for s in db.signals()}
    related: list[RelatedView] = []
    for link in db.links(filing.key):
        other = link.b if link.a == filing.key else link.a
        if other in signals:
            s = signals[other]
            related.append(
                RelatedView(key=other, kind=s.kind.value, title=s.title, url=s.url, reasons=list(link.reasons))
            )
        elif other_filing := db.filing(other):
            related.append(
                RelatedView(
                    key=other,
                    kind=f"filing:{other_filing.classification.category.value}",
                    title=other_filing.title,
                    url=None,
                    reasons=list(link.reasons),
                )
            )
    return ProjectDetail(
        summary=_summary(project),
        description=filing.project_description,
        review_decision=filing.review_decision.value,
        siret=filing.siret,
        classification=filing.classification.category.value,
        classification_method=filing.classification.method,
        power_basis=project.power.basis,
        documents=[DocumentView(title=d.title, url=d.url, role=d.role.value) for d in docs.values()],
        evidence=[
            EvidenceView(
                field=name,
                quote=ev.quote,
                locator=ev.locator,
                document_url=docs[ev.document_id].url if ev.document_id in docs else None,
            )
            for name, ev in sorted(filing.evidence.items())
        ],
        checks=[CheckView(name=c.name, status=c.status.value, detail=c.detail) for c in filing.checks],
        related=related,
    )


def zone_pressure(db: Database, lat: float, lon: float, radius_km: float = 15.0) -> ZoneSummary:
    projects = search_projects(db, lat=lat, lon=lon, radius_km=radius_km, limit=MAX_LIMIT)
    return ZoneSummary(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        project_count=len(projects),
        known_power_mw=round(sum(p.power_mw or 0 for p in projects), 1),
        projects_without_power=sum(1 for p in projects if p.power_mw is None),
        by_category=dict(Counter(p.category for p in projects)),
        projects=projects,
    )


def projects_near_substation(db: Database, substation: str, limit: int = 50) -> list[ProjectSummary]:
    return [_summary(p) for p in db.projects(categories=_categories(None), substation=substation)][:limit]


def dataset_info(db: Database) -> DatasetInfo:
    runs = {kind: info["finished_at"] for kind, info in db.last_runs().items()}
    return DatasetInfo(counts=db.counts(), last_runs=runs, coverage=COVERAGE, caveats=CAVEATS)


FIELD_LABELS_FR = {
    "name": "nom",
    "category": "catégorie",
    "developer": "porteur",
    "commune": "commune",
    "location_precision": "précision du lieu",
    "power_mw": "puissance (MW)",
    "power_mw_min": "puissance min (MW)",
    "energy_mwh": "énergie (MWh)",
    "power_status": "statut de la puissance",
    "substation": "poste",
    "status": "étape",
    "confidence": "confiance",
    "filings": "dossiers",
    "signals": "signaux liés",
}


def _change_summary(c: ProjectChange) -> str:
    if c.kind is ChangeKind.NEW:
        return f"Nouveau projet : {c.name}"
    if c.kind is ChangeKind.REMOVED:
        return f"Retiré (n'est plus un projet suivi) : {c.name}"
    parts = [f"{FIELD_LABELS_FR.get(f.field, f.field)} {f.before or '—'} → {f.after or '—'}" for f in c.fields]
    return f"{c.name} : " + " ; ".join(parts)


def _parse_since(since: str) -> str:
    """Accept an ISO date or datetime; raise ValueError otherwise."""
    try:
        return datetime.fromisoformat(since).isoformat() if "T" in since else date.fromisoformat(since).isoformat()
    except ValueError:
        raise ValueError(f"`since` must be an ISO date like 2026-10-01, got {since!r}") from None


def what_changed(
    db: Database,
    since: str,
    *,
    category: str | None = None,
    department: str | None = None,
    limit: int = 100,
) -> list[ChangeView]:
    rows = db.changes(
        since=_parse_since(since),
        categories=_categories(category),
        department=department,
        limit=max(1, min(limit, MAX_LIMIT)),
    )
    return [
        ChangeView(
            at=c.at.isoformat(),
            kind=c.kind.value,
            project_id=c.project_id,
            name=c.name,
            category=c.category.value,
            department=c.department,
            summary=_change_summary(c),
            fields=[FieldChangeView(field=f.field, before=f.before, after=f.after) for f in c.fields],
        )
        for c in rows
    ]
