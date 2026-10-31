"""DREAL « examen au cas par cas »: listing pages (8 articles each) -> article pages -> PDFs (form, decision).

Lessons: listing titles are truncated « (…) », so screening titles is only a first cut; deep pagination of the
pre-2025 archive takes ~30 s per page on the server side, so cap it with `max_offset`.
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from radar.domain.enums import SourceKind
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.quality.audit import decide
from radar.sources.base import CollectReport
from radar.sources.coverage import listing_period, load_scope
from radar.sources.dossier import (
    DEFAULT_DOSSIER_LIMITS,
    DossierLimits,
    collect_dossier,
    dossier_number,
    is_document_url,
)
from radar.sources.html import links
from radar.sources.regions import Region
from radar.store.blobs import BlobStore
from radar.store.db import Database

PAGE_SIZE = 8
_ARTICLE = re.compile(r"-a\d+\.html$")
_OFFSET = re.compile(r"debut_listearticles=(\d+)")


@dataclass(frozen=True)
class Article:
    url: str
    title: str


def parse_listing(page: str, base_url: str) -> list[Article]:
    seen: dict[str, Article] = {}
    for url, text, _ in links(page, base_url):
        if (
            _ARTICLE.search(urlparse(url).path)
            and text.strip()
            and "mentions-legales" not in url
            and "accessibilite" not in url
        ):
            seen.setdefault(url, Article(url=url, title=text))
    return list(seen.values())


def last_offset(page: str) -> int:
    return max((int(x) for x in _OFFSET.findall(page)), default=0)


def parse_article_pdfs(page: str, article_url: str) -> list[str]:
    return sorted({url for url, _, _ in links(page, article_url) if is_document_url(url)})


def _require_source(region: Region) -> None:
    if not region.dreal_base:
        raise ValueError("DREAL source is not configured for this region")


def iter_articles(http: PoliteHttp, region: Region, listing: str, max_offset: int) -> Iterator[Article]:
    _require_source(region)
    first = http.get_text(region.dreal_base + listing)
    yield from parse_listing(first, region.dreal_base)
    for offset in range(PAGE_SIZE, min(last_offset(first), max_offset) + 1, PAGE_SIZE):
        page = http.get_text(f"{region.dreal_base}{listing}?debut_listearticles={offset}")
        yield from parse_listing(page, region.dreal_base)


def collect_dreal(
    http: PoliteHttp,
    db: Database,
    blobs: BlobStore,
    region: Region,
    listings: tuple[str, ...],
    max_offset: int,
    max_articles: int | None = None,
    screen_terms: re.Pattern[str] | None = None,
    *,
    backfill: bool = False,
    max_pages: int = 30,
    restart: bool = False,
    dossier_limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
) -> CollectReport:
    """Screen titles (layer 1), download the PDFs of kept articles and of the audit sample, ingest them. Idempotent.
    Every listed item is recorded (`listing_items`) so the screen's recall can be estimated later."""
    _require_source(region)
    if max_offset < 0 or max_pages <= 0 or (max_articles is not None and max_articles <= 0):
        raise ValueError("Collection budgets must be positive (max-offset may be zero)")
    report = CollectReport()
    for listing in listings:
        scope = load_scope(
            db,
            "dreal",
            region.slug,
            listing_period(listing, region.dreal_periods),
            listing,
            "historical" if backfill else "recent",
        )
        scope.save(db, "running")
        try:
            first = http.get_text(region.dreal_base + listing)
            signature = hashlib.sha256(
                "\n".join(a.url for a in parse_listing(first, region.dreal_base)).encode()
            ).hexdigest()
            if not backfill or restart or scope.signature != signature:
                scope.next_offset, scope.pages, scope.completed_at = 0, 0, None
            scope.signature = signature
            last = last_offset(first)
            pages_this_run = 0
            while scope.next_offset <= last:
                offset = scope.next_offset
                if (backfill and pages_this_run >= max_pages) or (not backfill and offset > max_offset):
                    scope.save(db, "partial", "page_budget")
                    break
                page = (
                    first
                    if offset == 0
                    else http.get_text(f"{region.dreal_base}{listing}?{urlencode({'debut_listearticles': offset})}")
                )
                articles = parse_listing(page, region.dreal_base)
                if not articles:
                    scope.save(db, "partial", "empty_or_unrecognized_listing")
                    report.errors.append(f"empty or unrecognized listing: {listing} offset {offset}")
                    break
                errors_before = len(report.errors)
                for article in articles:
                    report.seen += 1
                    kept, audit = decide(article.title, article.url, extra=screen_terms)
                    with db.transaction():
                        db.record_listing_item(
                            article.url, SourceKind.DREAL_CASE_BY_CASE.value, article.title, kept, audit
                        )
                    if not kept and audit is None:
                        continue
                    if max_articles is not None and report.kept + report.audited >= max_articles:
                        scope.save(db, "partial", "item_budget")
                        return report
                    report.kept += kept
                    report.audited += audit is not None
                    page = http.get_text(article.url)
                    number = dossier_number(
                        db, SourceKind.DREAL_CASE_BY_CASE, region.slug, article.url, article.title, page
                    )
                    dossier = collect_dossier(
                        http,
                        db,
                        blobs,
                        source=SourceKind.DREAL_CASE_BY_CASE,
                        region=region.slug,
                        number=number,
                        root_url=article.url,
                        title=article.title,
                        audit=audit,
                        initial_page=page,
                        limits=dossier_limits,
                    )
                    report.documents += dossier.documents
                    report.errors.extend(dossier.errors)
                    if dossier.state != "complete" and not dossier.errors:
                        report.errors.append(f"incomplete dossier: {article.url}")
                if len(report.errors) != errors_before:
                    scope.save(db, "partial", "document_errors")
                    break
                scope.next_offset += PAGE_SIZE
                scope.pages += 1
                pages_this_run += 1
                scope.save(db, "complete" if scope.next_offset > last else "partial")
            else:
                scope.save(db, "complete", refresh_completion=pages_this_run > 0)
        except SiteBlockedError as exc:
            scope.save(db, "blocked", str(exc))
            raise
        except Exception as exc:
            scope.save(db, "failed", str(exc))
            raise
    return report
