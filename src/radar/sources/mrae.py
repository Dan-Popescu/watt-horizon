"""MRAe (regional environmental authority): yearly « Avis rendus sur projets » page listing every opinion PDF.

Mostly useful for projects SUBJECT to an impact study (big data centers, some storage sites). The link text is
short (« 013143/AP pdf - 1.5 Mio »): the project title sits in the text just before each link.
"""

import re
from dataclasses import dataclass

from radar.classification.rules import screen
from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.ids import normalize_filing_number, sha256
from radar.net.http import PoliteHttp
from radar.pipeline.ingest import ingest_pdf
from radar.sources.base import CollectReport
from radar.sources.html import links, strip_tags
from radar.sources.regions import Region
from radar.store.blobs import BlobStore
from radar.store.db import Database


@dataclass(frozen=True)
class Opinion:
    pdf_url: str
    reference: str  # link text, e.g. "013143/AP"
    context: str  # project title and requester, from the text preceding the link


def parse_opinions(page: str, page_url: str) -> list[Opinion]:
    opinions: list[Opinion] = []
    previous_end = 0
    for url, text, pos in links(page, page_url):
        if not url.lower().endswith(".pdf"):
            continue
        context = strip_tags(page[max(previous_end, pos - 1500) : pos])[-500:]
        reference = re.sub(r"\s*pdf\s*-.*$", "", text, flags=re.IGNORECASE).strip()
        opinions.append(Opinion(pdf_url=url, reference=reference, context=context))
        previous_end = pos
    return opinions


def collect_mrae(
    http: PoliteHttp, db: Database, blobs: BlobStore, region: Region, year: int, max_documents: int | None = None
) -> CollectReport:
    report = CollectReport()
    page_url = region.mrae_pages.get(year)
    if page_url is None:
        report.errors.append(f"no MRAe page configured for {region.slug} {year}")
        return report
    for op in parse_opinions(http.get_text(page_url), page_url):
        report.seen += 1
        if not screen(op.context, op.reference).keep:
            continue
        if max_documents is not None and report.documents >= max_documents:
            break
        report.kept += 1
        data = http.get_bytes(op.pdf_url)
        number = normalize_filing_number(op.reference) or "mrae-" + sha256(op.pdf_url.encode())[:10]
        with db.transaction():
            ingest_pdf(
                db,
                blobs,
                data=data,
                source=SourceKind.MRAE_OPINION,
                region=region.slug,
                number=number,
                url=op.pdf_url,
                title=op.context[-300:],
                filename=op.pdf_url.rsplit("/", 1)[-1],
                role=DocumentRole.OPINION,
            )
        report.documents += 1
    return report
