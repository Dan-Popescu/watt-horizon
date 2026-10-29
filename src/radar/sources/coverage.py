"""Evidence about enumerated source scopes, distinct from document/project recall."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from radar.sources.regions import Region
from radar.store.db import Database


def listing_period(listing: str) -> str:
    match = re.match(r"(avant-)?(\d{4})", listing)
    if match:
        return f"before-{match[2]}" if match[1] else match[2]
    return "unspecified"


@dataclass
class Coverage:
    source: str
    region: str
    period: str
    scope: str
    mode: str
    state: str = "unvisited"
    next_offset: int = 0
    pages: int = 0
    updated_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    signature: str | None = None

    def save(self, db: Database, state: str, error: str | None = None, *, refresh_completion: bool = True) -> None:
        self.state, self.error = state, error
        self.updated_at = datetime.now(UTC).isoformat()
        if state == "complete" and refresh_completion:
            self.completed_at = self.updated_at
        with db.transaction():
            db.save_collection_coverage(asdict(self))


def load_scope(db: Database, source: str, region: str, period: str, scope: str, mode: str) -> Coverage:
    for row in db.collection_coverage(region):
        if (row["source"], row["period"], row["scope"], row["mode"]) == (source, period, scope, mode):
            return Coverage(**row)
    return Coverage(source, region, period, scope, mode)


def inventory(
    db: Database, region: Region, years: list[int], *, stale_days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    """Expose missing configurations and unvisited scopes; completion covers listed pages only."""
    if stale_days <= 0:
        raise ValueError("stale-days must be positive")
    now = now or datetime.now(UTC)
    configured: list[Coverage] = []
    covered_years = {listing_period(x) for x in region.dreal_listings}
    for listing in region.dreal_listings:
        for mode in ("historical", "recent"):
            configured.append(load_scope(db, "dreal", region.slug, listing_period(listing), listing, mode))
    for year in years:
        if str(year) not in covered_years and not any(
            x.startswith("before-") and year < int(x.split("-")[1]) for x in covered_years
        ):
            configured.append(Coverage("dreal", region.slug, str(year), "", "historical", "unconfigured"))
        url = region.mrae_pages.get(year)
        configured.append(
            load_scope(db, "mrae", region.slug, str(year), url, "annual")
            if url
            else Coverage("mrae", region.slug, str(year), "", "annual", "unconfigured")
        )
    for department in region.departments:
        root = region.prefectures.get(department)
        configured.append(
            load_scope(db, "prefectures", region.slug, "all", root, "restricted")
            if root
            else Coverage("prefectures", region.slug, "all", department, "restricted", "unconfigured")
        )
    rows = []
    for item in configured:
        row = asdict(item)
        freshness_at = item.completed_at if item.state == "complete" and item.completed_at else item.updated_at
        row["stale"] = bool(freshness_at and now - datetime.fromisoformat(freshness_at) > timedelta(days=stale_days))
        row["status"] = "stale" if row["stale"] and item.state == "complete" else item.state
        rows.append(row)
    return {
        "region": region.slug,
        "years": years,
        "scopes": rows,
        "as_of": now.isoformat(),
        "caveat": "Couverture des pages configurées, pas une mesure d’exhaustivité des projets ou d’Internet.",
        "prefectures": "Périmètre de navigation restreint ; aucune couverture annuelle complète déduite.",
        "dossiers": db.dossier_summary(region.slug),
    }


def default_years() -> list[int]:
    return [date.today().year - 1, date.today().year]
