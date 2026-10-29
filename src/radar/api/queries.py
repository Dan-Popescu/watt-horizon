"""Read-only queries over the database. Pure functions of (Database, arguments) -> view models."""

import base64
import hashlib
import json
import math
from collections.abc import Iterable, Iterator
from datetime import date, datetime

from radar.api.views import (
    ChangeView,
    CheckView,
    DatasetInfo,
    DocumentView,
    EvidenceView,
    FieldChangeView,
    ProjectDetail,
    ProjectPage,
    ProjectSummary,
    RelatedView,
    ZoneSummary,
)
from radar.domain.enums import TARGET_CATEGORIES, Category, ChangeKind
from radar.domain.geo import haversine_km
from radar.domain.models import Project, ProjectChange
from radar.store.db import Database, ProjectFilters

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


def iter_project_summaries(
    db: Database,
    *,
    category: str | None = None,
    department: str | None = None,
) -> Iterator[ProjectSummary]:
    """Stream a complete filtered export through the same category policy as paged reads."""
    rows = db.iter_projects(categories=_categories(category), department=department)
    try:
        for project in rows:
            yield _summary(project)
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()


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
    near = _near(lat, lon, radius_km)
    rows = db.projects(
        categories=_categories(category),
        department=department,
        near=near,
        min_mw=min_mw,
        since=_parse_since(since) if since else None,
        commune=commune,
        limit=max(1, min(limit, MAX_LIMIT)),
    )
    return [_summary(p, _distance(p, near)) for p in rows]


def _near(lat: float | None, lon: float | None, radius: float) -> tuple[float, float, float] | None:
    if lat is None and lon is None:
        return None
    if lat is None or lon is None or not all(math.isfinite(v) for v in (lat, lon, radius)):
        raise ValueError("Latitude, longitude et rayon doivent être des nombres finis")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or radius < 0:
        raise ValueError("Coordonnées ou rayon hors limites")
    return lat, lon, radius


def _distance(project: Project, near: tuple[float, float, float] | None) -> float | None:
    if near is None or project.location.lat is None or project.location.lon is None:
        return None
    return haversine_km(near[0], near[1], project.location.lat, project.location.lon)


def project_page(
    db: Database,
    *,
    category: str | None = None,
    categories: list[str] | None = None,
    department: str | None = None,
    commune: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float = 20.0,
    min_mw: float | None = None,
    since: str | None = None,
    query: str | None = None,
    bounds: tuple[float, float, float, float] | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> ProjectPage:
    """Bounded, stable keyset pages; complete totals are independent of the displayed page."""
    near = _near(lat, lon, radius_km)
    if min_mw is not None and not math.isfinite(min_mw):
        raise ValueError("La puissance doit être un nombre fini")
    if bounds is not None:
        west, south, east, north = bounds
        if not all(math.isfinite(v) for v in bounds) or not (
            -180 <= west <= east <= 180 and -90 <= south <= north <= 90
        ):
            raise ValueError("Zone géographique invalide")
    filters: ProjectFilters = {
        "categories": sorted({Category(c).value for c in categories})
        if categories is not None
        else sorted(_categories(category)),
        "department": department,
        "commune": commune,
        "near": near,
        "min_mw": min_mw,
        "since": _parse_since(since) if since else None,
        "query": query,
        "bounds": bounds,
    }
    identity = hashlib.sha256(
        json.dumps([filters, db.project_revision(), str(db.path.resolve())], sort_keys=True).encode()
    ).hexdigest()[:24]
    after: tuple[float | None, str] | None = None
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError
            value, key, previous = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (
                previous != identity
                or not isinstance(key, str)
                or (value is not None and (not isinstance(value, (float, int)) or not math.isfinite(value)))
            ):
                raise ValueError
            if near and value is None:
                raise ValueError
            after = value, key
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Curseur invalide pour ces filtres") from exc
    size = max(1, min(limit, MAX_LIMIT))
    rows = db.projects(**filters, limit=size + 1, after=after)
    more = len(rows) > size
    shown = rows[:size]
    next_cursor = None
    if more and shown:
        last = shown[-1]
        rank = _distance(last, near) if near else last.power.mw
        next_cursor = base64.urlsafe_b64encode(json.dumps([rank, last.id, identity]).encode()).decode()
    return ProjectPage(
        items=[_summary(p, _distance(p, near)) for p in shown], next_cursor=next_cursor, **db.project_stats(**filters)
    )


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
    links = db.links(filing.key)
    signals = {s.key: s for s in db.signals(lk.b if lk.a == filing.key else lk.a for lk in links)}
    related: list[RelatedView] = []
    for link in links:
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
        versions=filing.versions,
        power_basis=project.power.basis,
        documents=[
            DocumentView(title=d.title, url=d.url, role=d.role.value, read_by=filing.reading.get(d.id, ""))
            for d in docs.values()
        ],
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
    page = project_page(db, lat=lat, lon=lon, radius_km=radius_km, limit=MAX_LIMIT)
    return ZoneSummary(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        project_count=page.total,
        known_power_mw=page.known_power_mw,
        projects_without_power=page.projects_without_power,
        by_category=page.by_category,
        projects=page.items,
        next_cursor=page.next_cursor,
    )


def projects_near_substation(db: Database, substation: str, limit: int = 50) -> list[ProjectSummary]:
    return [
        _summary(p)
        for p in db.projects(categories=_categories(None), substation=substation, limit=max(1, min(limit, MAX_LIMIT)))
    ]


def dataset_info(db: Database) -> DatasetInfo:
    runs = {kind: info["finished_at"] for kind, info in db.last_runs().items()}
    return DatasetInfo(
        counts=db.counts(), last_runs=runs, coverage=COVERAGE, caveats=CAVEATS, departments=db.project_departments()
    )


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


VALUE_LABELS_FR: dict[str, dict[str, str]] = {
    "confidence": {"high": "fiable", "medium": "assez fiable", "review": "à vérifier"},
    "power_status": {"known": "déclarée", "estimated": "estimée", "unknown": "inconnue"},
    "location_precision": {"exact": "GPS du dossier", "commune": "centre de la commune", "none": "inconnue"},
    "category": {
        "bess": "batteries",
        "bess_hybrid": "batteries + solaire/éolien",
        "storage_substation": "poste pour batteries",
        "datacenter": "data center",
    },
}


def _value_fr(field: str, value: str | None) -> str:
    return VALUE_LABELS_FR.get(field, {}).get(value or "", value or "—")


def _change_summary(c: ProjectChange) -> str:
    if c.kind is ChangeKind.NEW:
        return f"Nouveau projet : {c.name}"
    if c.kind is ChangeKind.REMOVED:
        return f"Retiré (n'est plus un projet suivi) : {c.name}"
    parts = [
        f"{FIELD_LABELS_FR.get(f.field, f.field)} {_value_fr(f.field, f.before)} → {_value_fr(f.field, f.after)}"
        for f in c.fields
    ]
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
            fields=[
                FieldChangeView(
                    field=f.field, label=FIELD_LABELS_FR.get(f.field, f.field), before=f.before, after=f.after
                )
                for f in c.fields
            ],
        )
        for c in rows
    ]
