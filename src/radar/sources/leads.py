"""Retain unlocated clues and corroborate only admitted official sources with downloaded documents."""

import re
from collections.abc import Sequence
from time import monotonic
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from radar.domain.departments import DEPARTMENTS
from radar.domain.enums import SourceKind
from radar.domain.ids import filing_key, normalize_filing_number
from radar.domain.privacy import PersonScrubber
from radar.domain.text import fold
from radar.net.http import PoliteHttp, RequestBudgetError, SiteBlockedError
from radar.reading.pdf import read_pdf
from radar.sources.dossier import DEFAULT_DOSSIER_LIMITS, DossierLimits, collect_dossier, dossier_number
from radar.sources.html import strip_tags
from radar.sources.regions import Region
from radar.sources.search import DEFAULT_OFFICIAL_DOMAINS, allowed_url, clean_url, validate_domains
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.store.discovery import DiscoveryStore


def remember_press_lead(
    store: DiscoveryStore,
    *,
    region: str,
    url: str,
    title: str,
    summary: str,
    feed_url: str,
) -> str | None:
    """Caller owns transaction. A place-free press clue is kept outside signals/projects."""
    canonical = clean_url(url)
    if canonical is None:
        return None
    scrubber = PersonScrubber()
    return store.add_lead(
        region=region,
        url=canonical,
        title=scrubber.scrub(title) or "",
        snippet=scrubber.scrub(summary) or "",
        origin="press",
        source_url=feed_url,
    )


def _source_for(url: str, region: Region, explicit: SourceKind | None) -> SourceKind | None:
    if explicit is not None:
        if explicit not in {SourceKind.DREAL_CASE_BY_CASE, SourceKind.MRAE_OPINION, SourceKind.PREFECTURE}:
            raise ValueError("Resolve requires an administrative source, never a press or registry assertion")
        return explicit
    host = urlparse(url).hostname
    if host == urlparse(region.dreal_base).hostname:
        return SourceKind.DREAL_CASE_BY_CASE
    if host in {urlparse(root).hostname for root in region.prefectures.values()}:
        return SourceKind.PREFECTURE
    if host in {urlparse(page).hostname for page in region.mrae_pages.values()}:
        return SourceKind.MRAE_OPINION  # Region evidence is still required before ingestion.
    # A national host alone does not establish which regional case is concerned.
    return None


def _region_supported(text: str, region: Region) -> bool:
    normalized = fold(text)
    if fold(region.slug.replace("-", " ")) in normalized:
        return True
    for department in region.departments:
        name = DEPARTMENTS.get(department)
        if name and re.search(
            r"\bdepartement\s+(?:de |du |des |d |de l )?" + re.escape(fold(name)) + r"\b", normalized
        ):
            return True
    return False


def _official_context(page: str, title: str) -> str:
    """National navigation/footer links cannot establish the project's region."""
    scoped = re.search(r"<main\b[^>]*>(.*?)</main>|<article\b[^>]*>(.*?)</article>", page, re.I | re.S)
    if scoped is None:
        return title
    body = re.sub(r"<(nav|header|footer|script|style)\b[^>]*>.*?</\1>", " ", scoped[1] or scoped[2], flags=re.I | re.S)
    return title + " " + strip_tags(body)


def _requires_source_collector(url: str, title: str, region: Region) -> bool:
    parsed = urlparse(url)
    configured = [
        *region.mrae_pages.values(),
        *(urljoin(region.dreal_base, listing) for listing in region.dreal_listings),
    ]
    if any((parsed.hostname, parsed.path) == (urlparse(root).hostname, urlparse(root).path) for root in configured):
        return True
    normalized = fold(title)
    if re.search(r"\b(?:archives|annee|liste|avis rendus|decisions prises)\b", normalized):
        return True
    return not re.search(
        r"\b(?:projet|demande|decision|enquete|consultation|permis|centrale|installation)\b", normalized
    )


def resolve_leads(
    http: PoliteHttp,
    db: Database,
    blobs: BlobStore,
    *,
    region: Region,
    official_domains: Sequence[str] = DEFAULT_OFFICIAL_DOMAINS,
    source: SourceKind | None = None,
    limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
    max_leads: int = 5,
    max_requests: int = 30,
    max_attempts_per_lead: int = 3,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Download admitted official pieces; resolved means source retrieved, not project verified.

    Query titles and snippets never become DocumentRecord titles or extracted facts.
    Unknown hosts/regions remain unconfirmed until a source is explicitly qualified.
    Hold the application's writer lock before recovering or processing pending work.
    """
    if not 0 < max_leads <= 1000 or max_requests <= 0 or max_attempts_per_lead <= 0:
        raise ValueError("Lead and HTTP budgets must be positive and bounded")
    domains = validate_domains(official_domains)
    store = DiscoveryStore(db.conn)
    with db.transaction():
        store.recover_interrupted(region.slug)
    resolved = partial = 0
    stopped: str | None = None
    errors: list[str] = []
    previous_interval = http.global_interval_s
    with http.request_budget(max_requests) as global_budget:
        try:
            for lead in store.leads(region.slug, limit=max_leads, retry_failed=retry_failed):
                if lead["attempts"] >= max_attempts_per_lead:
                    with db.transaction():
                        store.lead_state(lead["id"], "budget", error="cumulative_lead_attempt_budget")
                    continue
                if not allowed_url(lead["url"], domains):
                    with db.transaction():
                        store.lead_state(lead["id"], "unconfirmed", error="outside_qualified_official_domains")
                    continue
                selected_source = _source_for(lead["url"], region, source)
                if selected_source is None:
                    with db.transaction():
                        store.lead_state(lead["id"], "unconfirmed", error="source_region_requires_qualification")
                    continue
                if selected_source == SourceKind.PREFECTURE:
                    http.global_interval_s = max(previous_interval, 5)
                else:
                    http.global_interval_s = previous_interval
                with db.transaction():
                    store.lead_state(lead["id"], "running")
                    attempt = store.start_attempt("resolve", lead["id"])
                started = monotonic()

                def record_request(attempt_id: int = attempt) -> None:
                    with db.transaction():
                        store.record_request(attempt_id)

                request_count = 0
                case_key = None
                try:
                    with http.request_budget(max_requests, on_request=record_request) as request_budget:
                        try:
                            page, official_title = None, ""
                            data = http.get_bytes(lead["url"], same_origin_redirects=True)
                            context = ""
                            if data.lstrip().startswith(b"%PDF-"):
                                # Text is inspected only for source-region admission, never stored as a search fact.
                                context = read_pdf(data).text
                            else:
                                page = data.decode("utf-8", errors="replace")
                                heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.I | re.S) or re.search(
                                    r"<title\b[^>]*>(.*?)</title>", page, re.I | re.S
                                )
                                if heading:
                                    official_title = PersonScrubber().scrub(strip_tags(heading[1])) or ""
                                context = _official_context(page, official_title)
                                if _requires_source_collector(lead["url"], official_title, region):
                                    with db.transaction():
                                        store.lead_state(
                                            lead["id"],
                                            "unconfirmed",
                                            error="listing_requires_qualified_source_collector",
                                        )
                                        store.finish_attempt(
                                            attempt,
                                            "unconfirmed",
                                            requests=request_budget.requests,
                                            duration_s=monotonic() - started,
                                        )
                                    continue
                            region_host = urlparse(lead["url"]).hostname in {
                                urlparse(region.dreal_base).hostname,
                                *(urlparse(root).hostname for root in region.prefectures.values()),
                            }
                            if not region_host and not _region_supported(context, region):
                                with db.transaction():
                                    store.lead_state(
                                        lead["id"], "unconfirmed", error="official_source_does_not_establish_region"
                                    )
                                    store.finish_attempt(
                                        attempt,
                                        "unconfirmed",
                                        requests=request_budget.requests,
                                        duration_s=monotonic() - started,
                                    )
                                continue
                            filename = unquote(urlparse(lead["url"]).path.rsplit("/", 1)[-1])
                            number = normalize_filing_number(filename) or dossier_number(
                                db, selected_source, region.slug, lead["url"], official_title, page or ""
                            )
                            case_key = filing_key(selected_source, region.slug, number)
                            report = collect_dossier(
                                http,
                                db,
                                blobs,
                                source=selected_source,
                                region=region.slug,
                                number=number,
                                root_url=lead["url"],
                                title=official_title,
                                initial_data=data,
                                limits=limits,
                                min_interval_s=5 if selected_source == SourceKind.PREFECTURE else None,
                            )
                        finally:
                            request_count = request_budget.requests
                    state = (
                        "resolved"
                        if report.documents and report.state == "complete"
                        else "partial"
                        if report.documents
                        else "unconfirmed"
                    )
                    with db.transaction():
                        store.lead_state(
                            lead["id"],
                            state,
                            filing_key=case_key,
                            documents=len(db.documents(case_key)),
                            error="; ".join(report.errors) or None,
                        )
                        store.finish_attempt(
                            attempt,
                            state,
                            requests=request_count,
                            results=report.documents,
                            duration_s=monotonic() - started,
                        )
                    resolved += state == "resolved"
                    partial += state == "partial"
                except RequestBudgetError as exc:
                    with db.transaction():
                        store.lead_state(
                            lead["id"],
                            "partial",
                            error=str(exc),
                            filing_key=case_key,
                            documents=len(db.documents(case_key)) if case_key else None,
                        )
                        store.finish_attempt(
                            attempt, "partial", requests=request_count, error=str(exc), duration_s=monotonic() - started
                        )
                    stopped = "physical_request_budget"
                    break
                except SiteBlockedError as exc:
                    with db.transaction():
                        store.lead_state(
                            lead["id"],
                            "blocked",
                            error=str(exc),
                            filing_key=case_key,
                            documents=len(db.documents(case_key)) if case_key else None,
                        )
                        store.finish_attempt(
                            attempt, "blocked", requests=request_count, error=str(exc), duration_s=monotonic() - started
                        )
                    stopped = str(exc)
                    break
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    errors.append(reason)
                    with db.transaction():
                        store.lead_state(
                            lead["id"],
                            "failed",
                            error=reason,
                            filing_key=case_key,
                            documents=len(db.documents(case_key)) if case_key else None,
                        )
                        store.finish_attempt(
                            attempt, "failed", requests=request_count, error=reason, duration_s=monotonic() - started
                        )
        finally:
            http.global_interval_s = previous_interval
    return {
        "resolved": resolved,
        "partial": partial,
        "physical_requests": global_budget.requests,
        "stopped": stopped,
        "errors": errors,
        "status": store.status(region.slug),
        "caveat": "Résolu signifie pièces officielles récupérées ; la pertinence et les faits restent à valider.",
    }
