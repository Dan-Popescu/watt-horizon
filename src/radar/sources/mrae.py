"""MRAe (regional environmental authority): yearly « Avis rendus sur projets » page listing every opinion PDF.

Mostly useful for projects SUBJECT to an impact study (big data centers, some storage sites). The link text is
short (« 013143/AP pdf - 1.5 Mio »): the project title sits in the text just before each link.
"""

import re
from dataclasses import dataclass

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.ids import normalize_filing_number, sha256
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.quality.audit import decide
from radar.sources.base import CollectReport
from radar.sources.coverage import load_scope
from radar.sources.dossier import (
    DEFAULT_DOSSIER_LIMITS,
    DossierLimits,
    canonical_url,
    collect_dossier,
    is_document_url,
    same_origin,
)
from radar.sources.html import links, strip_tags
from radar.sources.regions import Region
from radar.store.blobs import BlobStore
from radar.store.db import Database


@dataclass(frozen=True)
class Opinion:
    pdf_url: str
    reference: str  # link text, e.g. "013143/AP"
    context: str  # project title and requester, from the text preceding the link
    parent_reference: str | None = None


def parse_opinions(page: str, page_url: str) -> list[Opinion]:
    opinions: list[Opinion] = []
    previous_end = 0
    previous_context = ""
    previous_reference = ""
    for url, text, pos in links(page, page_url):
        if not same_origin(url, page_url) or not (
            is_document_url(url) or re.search(r"dossier|annex|pi[eè]ce|[eé]tude", text, re.I)
        ):
            continue
        between = page[previous_end:pos]
        adjacent = not re.sub(r"<br\s*/?>|&nbsp;|[\s,;:|/-]", "", between, flags=re.I)
        context = strip_tags(page[max(previous_end, pos - 1500) : pos])[-500:]
        if adjacent and previous_context:
            context = previous_context
        reference = re.sub(r"\s*pdf\s*-.*$", "", text, flags=re.IGNORECASE).strip()
        parent = previous_reference if adjacent and re.search(r"annex|pi[eè]ce|dossier complet", text, re.I) else None
        opinions.append(
            Opinion(pdf_url=canonical_url(url), reference=reference, context=context, parent_reference=parent)
        )
        end = page.find("</a>", pos)
        previous_end = end + 4 if end >= 0 else pos
        previous_context = context
        if normalize_filing_number(reference):
            previous_reference = reference
    return opinions


def collect_mrae(
    http: PoliteHttp,
    db: Database,
    blobs: BlobStore,
    region: Region,
    year: int,
    max_documents: int | None = None,
    *,
    dossier_limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
) -> CollectReport:
    if max_documents is not None and max_documents <= 0:
        raise ValueError("max-items must be positive")
    report = CollectReport()
    page_url = region.mrae_pages.get(year)
    if page_url is None:
        report.errors.append(f"no MRAe page configured for {region.slug} {year}")
        return report
    scope = load_scope(db, "mrae", region.slug, str(year), page_url, "annual")
    scope.save(db, "running")
    try:
        opinions = parse_opinions(http.get_text(page_url), page_url)
        if not opinions:
            scope.save(db, "partial", "empty_or_unrecognized_listing")
            report.errors.append(f"no recognized opinions: {page_url}")
            return report
        for op in opinions:
            report.seen += 1
            kept, audit = decide(f"{op.context} {op.reference}", op.pdf_url)
            with db.transaction():
                db.record_listing_item(
                    op.pdf_url, SourceKind.MRAE_OPINION.value, f"{op.context} {op.reference}", kept, audit
                )
            if not kept and audit is None:
                continue
            if max_documents is not None and report.documents >= max_documents:
                scope.save(db, "partial", "item_budget")
                return report
            report.kept += kept
            report.audited += audit is not None
            number = (
                normalize_filing_number(op.reference)
                or normalize_filing_number(op.parent_reference)
                or "mrae-" + sha256(op.pdf_url.encode())[:10]
            )
            remaining = max_documents - report.documents if max_documents is not None else dossier_limits.documents
            limits = DossierLimits(
                pages=dossier_limits.pages,
                documents=min(dossier_limits.documents, remaining),
                depth=dossier_limits.depth,
                links=dossier_limits.links,
                bytes=dossier_limits.bytes,
            )
            dossier = collect_dossier(
                http,
                db,
                blobs,
                source=SourceKind.MRAE_OPINION,
                region=region.slug,
                number=number,
                root_url=op.pdf_url,
                title=op.context[-300:],
                audit=audit,
                root_role=DocumentRole.OPINION
                if not re.search(r"annex|[eé]tude|pi[eè]ce", op.reference, re.I)
                else None,
                limits=limits,
            )
            report.documents += dossier.documents
            report.errors.extend(dossier.errors)
            if dossier.state != "complete" and not dossier.errors:
                report.errors.append(f"incomplete dossier: {op.pdf_url}")
        scope.pages = 1
        scope.save(db, "partial" if report.errors else "complete", "document_errors" if report.errors else None)
    except SiteBlockedError as exc:
        scope.save(db, "blocked", str(exc))
        raise
    except Exception as exc:
        scope.save(db, "failed", str(exc))
        raise
    return report
