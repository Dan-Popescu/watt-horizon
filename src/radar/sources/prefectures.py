"""Prefecture websites: ICPE declarations, public inquiries, permits for energy installations. NOT YET TESTED LIVE.

The 101 prefecture sites share one platform: our first crawl (5 parallel processes) got the platform to close
connections for hours on 2026-10-12. Rules: one sequential crawl for ALL prefectures, >= 5 s between requests,
only the relevant sections, a page budget per site, and stop everything at the first `SiteBlockedError`.
"""

import re
import urllib.parse
from dataclasses import dataclass

from radar.classification.rules import screen
from radar.domain.enums import SourceKind
from radar.domain.ids import sha256
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.pipeline.ingest import ingest_pdf
from radar.sources.base import CollectReport
from radar.sources.coverage import Coverage, load_scope
from radar.sources.html import links
from radar.sources.regions import Region
from radar.store.blobs import BlobStore
from radar.store.db import Database

START = "/Actions-de-l-Etat/Environnement"
FOLLOW = re.compile(
    r"installations-classees|ICPE|participation|enquete|permis|consultation|declaration|energie", re.IGNORECASE
)
SKIP = re.compile(
    r"/eau|chasse|natura|biodiversite|bruit|funeraire|minier|sage|eolien|photovolta|carriere", re.IGNORECASE
)
MIN_INTERVAL_S = 5.0


@dataclass(frozen=True)
class Candidate:
    department: str
    page_url: str
    title: str
    pdf_url: str


def parse_page(page: str, page_url: str, site_root: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (section pages to follow, (pdf_url, link_text) candidates passing the screen)."""
    follow, pdfs = [], []
    for url, text, _ in links(page, page_url):
        path = urllib.parse.urlparse(url).path
        if "/contenu/telechargement/" in path and screen(text, path).keep:
            pdfs.append((url, text))
        elif url.startswith(site_root + START) and FOLLOW.search(path) and not SKIP.search(path):
            follow.append(url.split("#")[0])
    return follow, pdfs


def crawl_site(http: PoliteHttp, department: str, site_root: str, max_pages: int) -> list[Candidate]:
    queue, seen, found = [site_root + START], set[str](), list[Candidate]()
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        follow, pdfs = parse_page(http.get_text(url, min_interval_s=MIN_INTERVAL_S), url, site_root)
        queue += [u for u in follow if u not in seen]
        found += [Candidate(department, url, text, pdf) for pdf, text in pdfs]
    return found


def collect_prefectures(
    http: PoliteHttp, db: Database, blobs: BlobStore, region: Region, max_pages_per_site: int
) -> CollectReport:
    if max_pages_per_site <= 0:
        raise ValueError("max-pages must be positive")
    report = CollectReport()
    scope: Coverage | None = None
    try:
        for dep, root in region.prefectures.items():
            scope = load_scope(db, "prefectures", region.slug, "all", root, "restricted")
            scope.save(db, "running")
            for c in crawl_site(http, dep, root, max_pages_per_site):
                report.kept += 1
                data = http.get_bytes(c.pdf_url, min_interval_s=MIN_INTERVAL_S)
                number = "pref-" + sha256(c.pdf_url.encode())[:10]  # no filing number on these pages
                with db.transaction():
                    ingest_pdf(
                        db,
                        blobs,
                        data=data,
                        source=SourceKind.PREFECTURE,
                        region=region.slug,
                        number=number,
                        url=c.pdf_url,
                        title=c.title,
                        filename=c.pdf_url.rsplit("/", 1)[-1],
                    )
                report.documents += 1
            scope.save(db, "partial", "restricted_navigation_or_page_budget")
    except SiteBlockedError as exc:
        if scope is not None:
            scope.save(db, "blocked", str(exc))
        report.stopped = str(exc)  # shared platform: stop for every prefecture, retry another day
    except Exception as exc:
        if scope is not None:
            scope.save(db, "failed", str(exc))
        raise
    return report
