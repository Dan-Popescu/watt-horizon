"""Prefecture websites: ICPE declarations, public inquiries, permits for energy installations. NOT YET TESTED LIVE.

The 101 prefecture sites share one platform: our first crawl (5 parallel processes) got the platform to close
connections for hours on 2026-10-12. Rules: one sequential crawl for ALL prefectures, >= 5 s between requests,
only the relevant sections, a page budget per site, and stop everything at the first `SiteBlockedError`.
"""

import re
import urllib.parse
from dataclasses import dataclass

from radar.domain.enums import SourceKind
from radar.domain.ids import sha256
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.quality.audit import decide
from radar.sources.base import CollectReport
from radar.sources.coverage import Coverage, load_scope
from radar.sources.dossier import DEFAULT_DOSSIER_LIMITS, DossierLimits, collect_dossier, is_document_url, same_origin
from radar.sources.html import links, strip_tags
from radar.sources.regions import Region
from radar.store.blobs import BlobStore
from radar.store.db import Database

START = "/Actions-de-l-Etat/Environnement"
FOLLOW = re.compile(
    r"installations-classees|ICPE|participation|enquete|permis|consultation|declaration|energie|eolien|photovolta",
    re.IGNORECASE,
)
SKIP = re.compile(r"/eau|chasse|natura|biodiversite|bruit|funeraire|minier|sage|carriere", re.IGNORECASE)
MIN_INTERVAL_S = 5.0


@dataclass(frozen=True)
class Candidate:
    department: str
    page_url: str
    title: str
    pdf_url: str
    audit: str | None = None
    page: str | None = None


def parse_page(page: str, page_url: str, site_root: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return same-site section pages and attachment candidates; screening is audited by the caller."""
    follow, pdfs = [], []
    for url, text, _ in links(page, page_url):
        path = urllib.parse.urlparse(url).path
        if not same_origin(url, site_root):
            continue
        if is_document_url(url):
            pdfs.append((url, text))
        elif url.startswith(site_root + START) and FOLLOW.search(path) and not SKIP.search(path):
            follow.append(url.split("#")[0])
    return follow, pdfs


def crawl_site(
    http: PoliteHttp,
    department: str,
    site_root: str,
    max_pages: int,
    db: Database | None = None,
    report: CollectReport | None = None,
) -> list[Candidate]:
    queue, seen, found = [site_root + START], set[str](), list[Candidate]()
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page = http.get_text(url, min_interval_s=MIN_INTERVAL_S)
        follow, pdfs = parse_page(page, url, site_root)
        heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.I | re.S)
        title = strip_tags(heading[1]) if heading else ""
        # A selected case page supplies context to neutral annexes, but a category listing does not.
        if re.search(r"\b(?:projet|demande|centrale|construction|permis|décision)\b", title, re.I):
            kept, audit = decide(title, url)
            if db is not None:
                with db.transaction():
                    db.record_listing_item(url, SourceKind.PREFECTURE.value, title, kept, audit)
            if report is not None:
                report.seen += 1
            if kept or audit is not None:
                found.append(Candidate(department, url, title, url, audit=audit, page=page))
                continue
        queue += [u for u in follow if u not in seen]
        for pdf, text in pdfs:
            kept, audit = decide(text, pdf)
            if db is not None:
                with db.transaction():
                    db.record_listing_item(pdf, SourceKind.PREFECTURE.value, text, kept, audit)
            if report is not None:
                report.seen += 1
            if kept or audit is not None:
                found.append(Candidate(department, url, text, pdf, audit=audit))
    return found


def collect_prefectures(
    http: PoliteHttp,
    db: Database,
    blobs: BlobStore,
    region: Region,
    max_pages_per_site: int,
    *,
    dossier_limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
) -> CollectReport:
    if max_pages_per_site <= 0:
        raise ValueError("max-pages must be positive")
    report = CollectReport()
    scope: Coverage | None = None
    previous_interval = http.global_interval_s
    http.global_interval_s = max(previous_interval, MIN_INTERVAL_S)
    try:
        for dep, root in region.prefectures.items():
            scope = load_scope(db, "prefectures", region.slug, "all", root, "restricted")
            scope.save(db, "running")
            for c in crawl_site(http, dep, root, max_pages_per_site, db, report):
                report.kept += c.audit is None
                report.audited += c.audit is not None
                number = "pref-" + sha256(c.pdf_url.encode())[:10]
                dossier = collect_dossier(
                    http,
                    db,
                    blobs,
                    source=SourceKind.PREFECTURE,
                    region=region.slug,
                    number=number,
                    root_url=c.pdf_url,
                    title=c.title,
                    audit=c.audit,
                    initial_page=c.page,
                    min_interval_s=MIN_INTERVAL_S,
                    limits=dossier_limits,
                )
                report.documents += dossier.documents
                report.errors.extend(dossier.errors)
                if dossier.state != "complete" and not dossier.errors:
                    report.errors.append(f"incomplete dossier: {c.pdf_url}")
            scope.save(db, "partial", "restricted_navigation_or_page_budget")
    except SiteBlockedError as exc:
        if scope is not None:
            scope.save(db, "blocked", str(exc))
        report.stopped = str(exc)  # shared platform: stop for every prefecture, retry another day
    except Exception as exc:
        if scope is not None:
            scope.save(db, "failed", str(exc))
        raise
    finally:
        http.global_interval_s = previous_interval
    return report
