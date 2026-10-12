"""DREAL « examen au cas par cas »: listing pages (8 articles each) -> article pages -> PDFs (form, decision).

Lessons: listing titles are truncated « (…) », so screening titles is only a first cut; deep pagination of the
pre-2025 archive takes ~30 s per page on the server side, so cap it with `max_offset`.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from radar.classification.rules import screen
from radar.domain.enums import SourceKind
from radar.domain.ids import normalize_filing_number
from radar.net.http import PoliteHttp
from radar.pipeline.ingest import ingest_pdf
from radar.sources.base import CollectReport
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
        if _ARTICLE.search(url) and len(text) > 25 and "mentions-legales" not in url and "accessibilite" not in url:
            seen.setdefault(url, Article(url=url, title=text))
    return list(seen.values())


def last_offset(page: str) -> int:
    return max((int(x) for x in _OFFSET.findall(page)), default=0)


def parse_article_pdfs(page: str, article_url: str) -> list[str]:
    return sorted({url for url, _, _ in links(page, article_url) if url.lower().endswith(".pdf")})


def iter_articles(http: PoliteHttp, region: Region, listing: str, max_offset: int) -> Iterator[Article]:
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
) -> CollectReport:
    """Screen titles (layer 1), download the PDFs of kept articles, ingest them. Idempotent."""
    report = CollectReport()
    for listing in listings:
        for article in iter_articles(http, region, listing, max_offset):
            report.seen += 1
            if not screen(article.title).keep:
                continue
            if max_articles is not None and report.kept >= max_articles:
                return report
            report.kept += 1
            for pdf_url in parse_article_pdfs(http.get_text(article.url), article.url):
                filename = pdf_url.rsplit("/", 1)[-1]
                number = normalize_filing_number(filename) or normalize_filing_number(article.title)
                if number is None:
                    report.errors.append(f"no filing number: {pdf_url}")
                    continue
                data = http.get_bytes(pdf_url)
                with db.transaction():
                    ingest_pdf(
                        db,
                        blobs,
                        data=data,
                        source=SourceKind.DREAL_CASE_BY_CASE,
                        region=region.slug,
                        number=number,
                        url=pdf_url,
                        title=article.title,
                        filename=filename,
                    )
                report.documents += 1
    return report
