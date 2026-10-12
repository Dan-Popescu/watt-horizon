"""Company register (API recherche-entreprises, SIRENE/RNE data): newly created project companies = earliest signal.

Developers create one company per site (« HPC DATA CENTER 41 », « DECADE STORAGE ASSETS 1 », « CS 116 »).
Sole traders are excluded (personal data; never project companies), as are activities a project company
would not declare (marching bands named « batterie fanfare », self-storage…).
"""

import re
import urllib.parse
from datetime import date
from typing import Any

from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.models import Signal
from radar.net.http import PoliteHttp
from radar.sources.base import CollectReport
from radar.store.db import Database

API = "https://recherche-entreprises.api.gouv.fr/search?q={q}&departement={dep}&per_page=25&page={page}"
SHEET = "https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}"
QUERIES: dict[Category, tuple[str, ...]] = {
    Category.BESS: ("storage", "stockage energie", "battery", "batteries", "bess", "flex energie"),
    Category.DATACENTER: ("data center", "datacenter", "campus ia", "hyperscale"),
}
NAME_PATTERNS: dict[Category, re.Pattern[str]] = {
    Category.BESS: re.compile(r"storage|stockage|batter|\bbess\b|flex", re.IGNORECASE),
    Category.DATACENTER: re.compile(r"data ?cent|campus|hyperscale|\bdc\b|cloud", re.IGNORECASE),
}
# Production/trading of electricity, data hosting, holdings, land rental, construction, engineering.
PLAUSIBLE_NAF = re.compile(r"^(?:35\.|63\.11|68\.20|70\.10|64\.20|41\.10|42\.22|71\.12)")


def to_signal(row: dict[str, Any], category: Category, since: date) -> Signal | None:
    name, created = row.get("nom_complet") or "", row.get("date_creation") or ""
    if not created or created < since.isoformat() or row.get("etat_administratif") != "A":
        return None
    if str(row.get("nature_juridique") or "").startswith("1") or not PLAUSIBLE_NAF.match(
        row.get("activite_principale") or ""
    ):
        return None
    if not NAME_PATTERNS[category].search(name):
        return None
    siege = row.get("siege") or {}
    parents = [d.get("denomination") for d in row.get("dirigeants") or [] if d.get("denomination")]
    return Signal(
        key=f"company:{row['siren']}",
        kind=SignalKind.COMPANY,
        source=SourceKind.COMPANY_REGISTRY,
        title=name,
        url=SHEET.format(siren=row["siren"]),
        observed_on=date.fromisoformat(created),
        developer=name,
        communes=(),
        department=siege.get("departement"),
        category_hint=category,
        payload={
            "naf": row.get("activite_principale") or "",
            "siege": siege.get("libelle_commune") or "",
            "parents": "; ".join(p for p in parents[:3] if p),
        },
    )


def collect_registry(
    http: PoliteHttp, db: Database, departments: tuple[str, ...], since: date, max_pages: int = 2
) -> CollectReport:
    report = CollectReport()
    for category, queries in QUERIES.items():
        for query in queries:
            for dep in departments:
                for page in range(1, max_pages + 1):
                    data = http.get_json(
                        API.format(q=urllib.parse.quote(query), dep=dep, page=page), min_interval_s=0.4
                    )
                    for row in data.get("results") or []:
                        report.seen += 1
                        if signal := to_signal(row, category, since):
                            with db.transaction():
                                db.upsert_signal(signal)
                            report.signals += 1
                    if page >= int(data.get("total_pages") or 1):
                        break
    return report
