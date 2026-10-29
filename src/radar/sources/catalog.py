"""Qualified source configuration and bounded discovery of observed annual pages.

Observed links remain candidates until an explicit, structurally checked adoption.
This catalog configures connectors; it never certifies coverage or human accuracy.
"""

import hashlib
import re
from collections import deque
from dataclasses import asdict
from datetime import UTC, date, datetime
from itertools import islice
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from radar.domain.departments import DEPARTMENTS
from radar.domain.text import fold
from radar.evaluation.partitions import atomic_json
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources.coverage import listing_period
from radar.sources.html import links, strip_tags
from radar.sources.regions import REGIONS, Region

Source = Literal["dreal", "mrae"]
_YEAR = re.compile(r"(?<![a-z0-9])(?:19|20)\d{2}(?!\d)")
_DREAL = re.compile(r"cas par cas|examen au cas")
_MRAE = re.compile(r"\bavis\b.{0,120}\bprojets?\b|\bprojets?\b.{0,120}\bavis\b")
_SECTION = re.compile(r"cas.par.cas|avis.{0,120}projet|projet.{0,120}avis", re.I)


def official_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".gouv.fr")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("Source URL must be an HTTPS government hostname without credentials or a custom port")
    return urldefrag(url)[0]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegionConfig(StrictModel):
    slug: str
    departments: tuple[str, ...]
    dreal_base: str
    dreal_listings: tuple[str, ...] = ()
    mrae_pages: dict[int, str] = Field(default_factory=dict)
    prefectures: dict[str, str] = Field(default_factory=dict)
    dreal_periods: dict[str, str] = Field(default_factory=dict)
    seeds: dict[Source, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_sources(self) -> "RegionConfig":
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", self.slug):
            raise ValueError("Region slug must contain lowercase words separated by hyphens")
        if (
            not self.departments
            or len(set(self.departments)) != len(self.departments)
            or set(self.departments) - DEPARTMENTS.keys()
        ):
            raise ValueError("Region requires unique known French department codes")
        base = official_url(self.dreal_base)
        if not base.endswith("/") or urlparse(base).query:
            raise ValueError("DREAL base must end in / and contain no query")
        for listing in self.dreal_listings:
            if (
                listing.startswith(("/", "http"))
                or ".." in unquote(listing).split("/")
                or urlparse(listing).fragment
                or urlparse(listing).query
            ):
                raise ValueError("DREAL listings must be relative to their configured base")
            if official_url(urljoin(base, listing)) != base + listing:
                raise ValueError("DREAL listing must remain under its configured base")
        if len(set(self.dreal_listings)) != len(self.dreal_listings):
            raise ValueError("Duplicate DREAL listing")
        if set(self.dreal_periods) - set(self.dreal_listings) or any(
            not re.fullmatch(r"(?:before-)?(?:19|20)\d{2}", period)
            or int(period.removeprefix("before-")) > date.today().year
            for period in self.dreal_periods.values()
        ):
            raise ValueError("Explicit DREAL periods require configured listings and non-future annual/archive periods")
        for year, url in self.mrae_pages.items():
            if not 1900 <= year <= date.today().year:
                raise ValueError("Configured annual sources cannot describe a future year")
            official_url(url)
        if set(self.prefectures) - set(self.departments):
            raise ValueError("Prefecture department does not belong to the configured region")
        for url in self.prefectures.values():
            official_url(url)
        for source, urls in self.seeds.items():
            for url in urls:
                official_url(url)
                if source == "dreal" and not url.startswith(base):
                    raise ValueError("DREAL discovery seed must remain under the configured DREAL base")
        return self

    def region(self) -> Region:
        return Region(**self.model_dump(exclude={"seeds"}))


class SourceCandidate(StrictModel):
    id: str
    region: str
    source: Source
    year: int
    url: str
    parent_url: str
    link_text: str
    observed_at: str
    parent_sha256: str
    state: Literal["pending", "adopted", "rejected", "blocked", "failed"] = "pending"
    checked_at: str | None = None
    target_sha256: str | None = None
    adopted_at: str | None = None
    qualification_error: str | None = None

    @model_validator(mode="after")
    def valid_observation(self) -> "SourceCandidate":
        expected = hashlib.sha256(f"{self.source}:{self.region}:{self.year}:{self.url}".encode()).hexdigest()[:24]
        if self.id != expected or not 1900 <= self.year <= date.today().year:
            raise ValueError("Candidate identity/year does not match an observed annual page")
        if not re.fullmatch(r"[0-9a-f]{64}", self.parent_sha256):
            raise ValueError("Candidate requires the parent page content hash")
        datetime.fromisoformat(self.observed_at)
        return self


class SourceCatalog(StrictModel):
    version: Literal[1] = 1
    regions: tuple[RegionConfig, ...]
    candidates: tuple[SourceCandidate, ...] = ()
    last_discovery: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_identities(self) -> "SourceCatalog":
        if len({r.slug for r in self.regions}) != len(self.regions):
            raise ValueError("Duplicate catalog region")
        if len({candidate.id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate source candidate")
        for candidate in self.candidates:
            if candidate.region not in {r.slug for r in self.regions}:
                raise ValueError("Candidate has an unknown region")
            official_url(candidate.url)
            official_url(candidate.parent_url)
        return self


def load_catalog(path: Path) -> SourceCatalog:
    if path.exists():
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Source catalog exceeds its 5 MiB configuration budget")
        return SourceCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    return SourceCatalog(regions=tuple(RegionConfig(**asdict(region)) for region in REGIONS.values()))


def save_catalog(path: Path, catalog: SourceCatalog) -> None:
    atomic_json(path, catalog.model_dump(mode="json"))


def configured_regions(path: Path) -> dict[str, Region]:
    return {**REGIONS, **{r.slug: r.region() for r in load_catalog(path).regions}}


def status(path: Path) -> dict[str, Any]:
    catalog = load_catalog(path)
    return {
        "path": str(path),
        "regions": [r.model_dump(mode="json") for r in catalog.regions],
        "candidates": [candidate.model_dump(mode="json") for candidate in catalog.candidates],
        "last_discovery": catalog.last_discovery,
        "caveat": "Une configuration adoptée ne prouve ni la couverture ni une validation humaine.",
    }


def import_regions(path: Path, configuration: Path) -> dict[str, Any]:
    """Add explicitly supplied regional seeds without replacing existing qualified mappings."""
    incoming = SourceCatalog.model_validate_json(configuration.read_text(encoding="utf-8"))
    if incoming.candidates or incoming.last_discovery:
        raise ValueError("Region import accepts configuration only, never fabricated observation or adoption records")
    catalog = load_catalog(path)
    regions = {r.slug: r for r in catalog.regions}
    for region in incoming.regions:
        if region.slug in regions:
            previous = regions[region.slug]
            if previous.model_dump(exclude={"seeds"}) != region.model_dump(exclude={"seeds"}):
                raise ValueError(
                    "Conflicting region configuration; add observed candidates instead of replacing mappings"
                )
            merged = {
                source: tuple(dict.fromkeys((*previous.seeds.get(source, ()), *region.seeds.get(source, ()))))
                for source in set(previous.seeds) | set(region.seeds)
            }
            region = region.model_copy(update={"seeds": merged})
        regions[region.slug] = region
    save_catalog(path, catalog.model_copy(update={"regions": tuple(regions.values())}))
    return {"regions": sorted(regions), "status": "configured_seeds", "live_verified": False}


def _html_candidate(url: str) -> bool:
    path = unquote(urlparse(url).path)
    return Path(path).suffix.lower() in {"", ".html", ".htm"} and not re.search(
        r"/telecharg|/download|/attachment", path, re.I
    )


def _get_page(http: PoliteHttp, url: str) -> str:
    data = http.get_bytes(url, same_origin_redirects=True)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("Source qualification page exceeds its 4 MiB parsing budget")
    return data.decode("utf-8", errors="replace")


def annual_links(
    page: str, page_url: str, region: RegionConfig, source: Source, *, max_links: int = 500
) -> list[tuple[int, str, str]]:
    """Find explicit same-origin annual links, never infer a URL from a year."""
    found: dict[str, tuple[int, str, str]] = {}
    origin = urlparse(page_url).netloc.lower()
    if max_links <= 0:
        raise ValueError("Annual link budget must be positive")
    for raw_url, label, _ in islice(links(page, page_url), max_links):
        url = urldefrag(raw_url)[0]
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != origin
            or parsed.query
            or parsed.username
            or not _html_candidate(url)
        ):
            continue
        combined = fold(f"{unquote(parsed.path)} {label}")
        years = set(_YEAR.findall(combined))
        if len(years) != 1 or re.search(r"\b(?:avant|before|depuis)\b", combined):
            continue
        year = int(next(iter(years)))
        if year > date.today().year:
            continue
        label_folded = fold(label)
        if source == "dreal":
            if not url.startswith(region.dreal_base) or not (
                _DREAL.search(combined) or (label_folded == str(year) and re.search(r"-r\d+\.html$", parsed.path))
            ):
                continue
        elif not (_MRAE.search(combined) or label_folded == str(year)):
            continue
        found[url] = year, url, label
    return list(found.values())


def discover(
    http: PoliteHttp, path: Path, region_slug: str, source: Source, *, years: tuple[int, ...] = (), max_pages: int = 8
) -> dict[str, Any]:
    """Discover candidates serially within known official origins; persist even a blocked partial attempt."""
    if not 1 <= max_pages <= 100 or any(not 1900 <= year <= date.today().year for year in years):
        raise ValueError("Discovery requires 1..100 pages and non-future years")
    catalog = load_catalog(path)
    region = next((r for r in catalog.regions if r.slug == region_slug), None)
    if region is None:
        raise ValueError("Configure regional seeds before discovering their annual pages")
    seeds = region.seeds.get(source) or (
        tuple(region.dreal_base + listing for listing in region.dreal_listings)
        if source == "dreal"
        else tuple(region.mrae_pages.values())
    )
    if not seeds:
        raise ValueError("No observed or explicitly configured seed for this source")
    queue = deque(seeds)
    seen: set[str] = set()
    configured_urls = (
        {region.dreal_base + listing for listing in region.dreal_listings}
        if source == "dreal"
        else set(region.mrae_pages.values())
    )
    observed_years: set[int] = set()
    candidates = {candidate.id: candidate for candidate in catalog.candidates}
    report: dict[str, Any] = {
        "region": region_slug,
        "source": source,
        "pages": 0,
        "new_candidates": 0,
        "status": "running",
        "link_budget_reached": False,
        "errors": [],
    }
    try:
        while queue and len(seen) < max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            page = _get_page(http, url)
            report["pages"] += 1
            page_digest = hashlib.sha256(page.encode()).hexdigest()
            page_links = list(islice(links(page, url), 501))
            if len(page_links) > 500:
                report["link_budget_reached"] = True
            for year, target, label in annual_links(page, url, region, source):
                observed_years.add(year)
                if target not in seen and len(queue) < 500:
                    queue.append(target)
                if years and year not in years:
                    continue
                if target in configured_urls:
                    continue  # Already configured URLs must not masquerade as pending discoveries.
                candidate_id = hashlib.sha256(f"{source}:{region_slug}:{year}:{target}".encode()).hexdigest()[:24]
                if candidate_id not in candidates and len(candidates) >= 10_000:
                    report["link_budget_reached"] = True
                    continue
                if candidate_id not in candidates:
                    candidates[candidate_id] = SourceCandidate(
                        id=candidate_id,
                        region=region_slug,
                        source=source,
                        year=year,
                        url=target,
                        parent_url=url,
                        link_text=label[:500],
                        observed_at=datetime.now(UTC).isoformat(),
                        parent_sha256=page_digest,
                    )
                    report["new_candidates"] += 1
            for target, label, _ in page_links[:500]:
                if len(queue) >= 500:
                    break
                if (
                    target not in seen
                    and urlparse(target).netloc.lower() == urlparse(url).netloc.lower()
                    and urlparse(target).scheme == "https"
                    and not urlparse(target).query
                    and _html_candidate(target)
                    and _SECTION.search(f"{target} {label}")
                ):
                    queue.append(urldefrag(target)[0])
        report["status"] = "partial" if queue or report["link_budget_reached"] else "finished"
    except SiteBlockedError as exc:
        report["status"], report["stopped"] = "blocked", str(exc)
    except Exception as exc:
        report["status"] = "failed"
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["observed_at"] = datetime.now(UTC).isoformat()
    report["observed_years"] = sorted(observed_years)
    report["unmatched_years"] = sorted(set(years) - observed_years)
    save_catalog(path, catalog.model_copy(update={"candidates": tuple(candidates.values()), "last_discovery": report}))
    return report


def qualify(page: str, url: str, region: RegionConfig, source: Source, year: int) -> None:
    """Check connector compatibility and regional/year identity before activating an observed candidate."""
    text = fold(strip_tags(page))
    heading = " ".join(re.findall(r"<(?:title|h1)\b[^>]*>(.*?)</(?:title|h1)>", page, re.I | re.S))
    identity = fold(f"{unquote(urlparse(url).path)} {strip_tags(heading)}")
    if str(year) not in _YEAR.findall(identity):
        raise ValueError("Annual source target does not establish the candidate year")
    if source == "dreal":
        from radar.sources.dreal import parse_listing

        articles = parse_listing(page, url)
        matches = sum(bool(_DREAL.search(fold(article.title))) for article in articles)
        if not url.startswith(region.dreal_base) or not articles or not matches or matches * 2 < len(articles):
            raise ValueError("Target is not a recognized DREAL case-by-case listing")
    else:
        from radar.sources.mrae import parse_opinions

        if (
            fold(region.slug) not in fold(strip_tags(heading))
            or not _MRAE.search(text)
            or not parse_opinions(page, url)
        ):
            raise ValueError("Target is not a recognized MRAe annual project-opinion page for this region")


def adopt(http: PoliteHttp, path: Path, candidate_id: str) -> dict[str, Any]:
    catalog = load_catalog(path)
    candidate = next((c for c in catalog.candidates if c.id == candidate_id), None)
    if candidate is None:
        raise ValueError("Only an observed source candidate may be adopted")
    region = next(r for r in catalog.regions if r.slug == candidate.region)
    page = ""
    try:
        page = _get_page(http, candidate.url)
        qualify(page, candidate.url, region, candidate.source, candidate.year)
        if candidate.source == "mrae":
            if candidate.year in region.mrae_pages and region.mrae_pages[candidate.year] != candidate.url:
                raise ValueError("Conflicting adopted annual MRAe page; explicit configuration review required")
            updated = region.model_copy(update={"mrae_pages": region.mrae_pages | {candidate.year: candidate.url}})
        else:
            listing = candidate.url.removeprefix(region.dreal_base)
            entries = tuple(dict.fromkeys((*region.dreal_listings, listing)))
            periods = region.dreal_periods | {listing: str(candidate.year)}

            def newest_first(value: str) -> tuple[int, int]:
                period = listing_period(value, periods)
                return (0, -int(period)) if period.isdigit() else (1, 0)

            # Daily collection uses [:2]; keep annual listings newest first, archives last.
            updated = region.model_copy(
                update={
                    "dreal_listings": tuple(sorted(entries, key=newest_first)),
                    "dreal_periods": periods,
                }
            )
        updated = RegionConfig.model_validate(updated.model_dump())
    except Exception as exc:
        failed = candidate.model_copy(
            update={
                "state": "blocked"
                if isinstance(exc, SiteBlockedError)
                else "rejected"
                if isinstance(exc, ValueError)
                else "failed",
                "checked_at": datetime.now(UTC).isoformat(),
                "target_sha256": hashlib.sha256(page.encode()).hexdigest() if page else None,
                "qualification_error": f"{type(exc).__name__}: {exc}",
            }
        )
        save_catalog(
            path,
            catalog.model_copy(
                update={
                    "candidates": tuple(failed if c.id == candidate.id else c for c in catalog.candidates),
                }
            ),
        )
        raise
    checked = candidate.model_copy(
        update={
            "state": "adopted",
            "checked_at": datetime.now(UTC).isoformat(),
            "target_sha256": hashlib.sha256(page.encode()).hexdigest(),
            "adopted_at": datetime.now(UTC).isoformat(),
            "qualification_error": None,
        }
    )
    save_catalog(
        path,
        catalog.model_copy(
            update={
                "regions": tuple(updated if r.slug == region.slug else r for r in catalog.regions),
                "candidates": tuple(checked if c.id == candidate.id else c for c in catalog.candidates),
            }
        ),
    )
    return {
        "candidate": checked.model_dump(mode="json"),
        "region": updated.model_dump(mode="json"),
        "qualification": "connector_structure_checked",
        "coverage_verified": False,
        "human_verified": False,
    }
