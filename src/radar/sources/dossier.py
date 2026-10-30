"""Bounded, same-origin traversal of the attachments of one selected administrative case.

The caller chooses the filing identity before traversal: neutral annexes cannot invent a second case.
The graph and the last scan outcome remain private operational evidence, including refused budgets.
"""

import re
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urldefrag, urlparse

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.ids import filing_key, normalize_filing_number, sha256
from radar.net.http import PoliteHttp, RequestBudgetError, SiteBlockedError
from radar.pipeline.ingest import ingest_pdf
from radar.reading.formats import detect_format
from radar.sources.html import links
from radar.store.blobs import BlobStore
from radar.store.db import Database, utcnow

_DOWNLOAD = re.compile(r"/contenu/telechargement/|/download(?:/|\b)|/telecharg|/attachment(?:/|\b)", re.I)
_ATTACHMENT = re.compile(
    r"annex|dossier|pi[eè]ce|document|t[eé]l[eé]charg|[eé]tude|formulaire|d[eé]cision|\bavis\b", re.I
)
_UNRELATED = re.compile(r"autres? (?:dossiers?|projets?)|retour|accueil|mentions|accessibilit|archives|recherche", re.I)


def canonical_url(url: str) -> str:
    return urldefrag(url)[0]


def same_origin(a: str, b: str) -> bool:
    left, right = urlparse(a), urlparse(b)
    return (
        left.scheme in {"http", "https"}
        and (left.scheme, left.netloc.lower()) == (right.scheme, right.netloc.lower())
        and left.username is None
        and right.username is None
    )


def is_document_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        unquote(parsed.path).lower().endswith((".pdf", ".docx", ".xlsx", ".zip"))
        or bool(_DOWNLOAD.search(parsed.path))
        or bool(re.search(r"(?:^|[=&])[^=&]*\.(?:pdf|docx|xlsx|zip)(?:&|$)", unquote(parsed.query), re.I))
    )


def attachment_links(
    page: str, page_url: str, root_url: str, *, max_links: int = 501, include_external: bool = False
) -> list[tuple[str, str]]:
    """Only selected-case attachment links; never a general whole-site crawl."""
    found: dict[str, str] = {}
    for raw, title, _ in links(page, page_url):
        url = canonical_url(raw)
        if (
            urlparse(url).scheme not in {"http", "https"}
            or (not include_external and not same_origin(url, root_url))
            or _UNRELATED.search(title)
        ):
            continue
        if is_document_url(url) or _ATTACHMENT.search(title):
            found.setdefault(url, title)
            if len(found) >= max_links:
                break
    return list(found.items())


@dataclass(frozen=True)
class DossierLimits:
    pages: int = 8
    documents: int = 24
    depth: int = 2
    links: int = 500
    bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.pages <= 0 or self.documents <= 0 or self.depth < 0 or self.links <= 0 or self.bytes <= 0:
            raise ValueError("Dossier budgets must be positive (depth may be zero)")


DEFAULT_DOSSIER_LIMITS = DossierLimits()


@dataclass
class DossierReport:
    state: str = "running"
    pages: int = 0
    documents: int = 0
    bytes: int = 0
    outside_scope: int = 0
    links: int = 0
    errors: list[str] = field(default_factory=list)
    updated_at: str = ""


def scan_dossier(
    http: PoliteHttp,
    db: Database,
    key: str,
    root_url: str,
    ingest: Callable[[bytes, str, str], str],
    *,
    initial_page: str | None = None,
    initial_data: bytes | None = None,
    limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
    min_interval_s: float | None = None,
) -> DossierReport:
    """Revisit a dossier serially; ingestion is idempotent and each response is bounded by PoliteHttp.

    Stop immediately on a site block. A limited or unrecognized traversal can never become complete.
    ``complete`` means enumerated recognized same-origin attachments, never every possible external annex.
    """
    root_url = canonical_url(root_url)
    if initial_page is not None and initial_data is not None:
        raise ValueError("Supply one initial dossier representation, never both text and bytes")
    queue = deque([(root_url, root_url, "", 0)])
    seen: set[str] = set()
    terminal: dict[str, tuple[str, str | None]] = {}
    queued = {root_url}
    report = DossierReport()
    limited = False

    def persist() -> None:
        report.updated_at = utcnow().isoformat()
        with db.transaction():
            db.save_dossier_scan(key, root_url, asdict(report))

    def edge(parent: str, url: str, state: str, document_id: str | None = None) -> None:
        with db.transaction():
            db.record_dossier_edge(key, root_url, parent, url, state, document_id)
            if state in {"document", "page", "invalid_document", "unsupported"}:
                terminal[url] = (state, document_id)
                db.settle_dossier_url(key, root_url, url, state, document_id)

    persist()
    try:
        while queue:
            parent, url, title, depth = queue.popleft()
            if url in seen:
                continue
            if report.documents >= limits.documents or (report.pages >= limits.pages and not is_document_url(url)):
                edge(parent, url, "budget")
                limited = True
                break
            seen.add(url)
            edge(parent, url, "fetching")
            data = (
                initial_data
                if url == root_url and initial_data is not None
                else initial_page.encode()
                if url == root_url and initial_page is not None
                else http.get_bytes(url, min_interval_s=min_interval_s, same_origin_redirects=True)
            )
            report.bytes += len(data)
            if report.bytes > limits.bytes:
                edge(parent, url, "budget")
                limited = True
                break
            try:
                kind = detect_format(data)
            except ValueError as exc:
                edge(parent, url, "invalid_document")
                report.errors.append(f"{url}: {exc}")
                continue
            if kind in {"pdf", "docx", "xlsx", "zip"}:
                try:
                    document_id = ingest(data, url, title)
                except ValueError as exc:
                    edge(parent, url, "invalid_document")
                    report.errors.append(f"{url}: {exc}")
                    continue
                report.documents += 1
                edge(parent, url, "document", document_id)
                if kind == "zip":
                    unresolved = [
                        item
                        for item in db.dossier_edges(key)
                        if item["root_url"].startswith(url) and item["state"] in {"budget", "unsupported"}
                    ]
                    if unresolved:
                        report.errors.append(f"archive contains unread or budget-limited members: {url}")
                continue
            if b"<" not in data[:512]:
                edge(parent, url, "unsupported")
                report.errors.append(f"unsupported response: {url}")
                continue
            if report.pages >= limits.pages:
                edge(parent, url, "budget")
                limited = True
                continue
            report.pages += 1
            edge(parent, url, "page")
            # A complete HTML document is evidence too; fragments containing only links remain navigation.
            if kind == "html":
                try:
                    document_id = ingest(data, url, title)
                except ValueError as exc:
                    report.errors.append(f"HTML document {url}: {exc}")
                else:
                    report.documents += 1
                    edge(parent, url, "document", document_id)
            children = attachment_links(
                data.decode("utf-8", errors="replace"), url, root_url, max_links=limits.links + 1, include_external=True
            )
            if len(children) > limits.links:
                limited = True
                children = children[: limits.links]
            for child, child_title in children:
                if report.links >= limits.links:
                    limited = True
                    break
                report.links += 1
                if not same_origin(child, root_url):
                    edge(url, child, "outside_scope")
                    report.outside_scope += 1
                    limited = True
                    continue
                if child in seen:
                    state, reference_id = terminal.get(child, ("visited", None))
                    edge(url, child, state, reference_id)
                    continue
                edge(url, child, "discovered")
                if depth >= limits.depth or len(queued) >= limits.links:
                    edge(url, child, "budget")
                    limited = True
                    continue
                if child not in queued:
                    queue.append((url, child, child_title, depth + 1))
                    queued.add(child)
        if not report.documents:
            report.errors.append(f"no recognized documents: {root_url}")
        report.state = "partial" if limited or report.errors else "complete"
    except RequestBudgetError as exc:
        report.state = "partial"
        report.errors.append(f"{type(exc).__name__}: {exc}")
        edge(parent, url, "budget")
        persist()
        raise
    except SiteBlockedError as exc:
        report.state = "blocked"
        report.errors.append(f"{type(exc).__name__}: {exc}")
        edge(parent, url, "blocked")
        persist()
        raise
    except Exception as exc:
        report.state = "failed"
        report.errors.append(f"{type(exc).__name__}: {exc}")
        edge(parent, url, "failed")
        persist()
        raise
    persist()
    return report


def dossier_number(db: Database, source: SourceKind, region: str, root_url: str, title: str, page: str) -> str:
    """A page's existing identity is immutable even when its attachment names change."""
    prefix = f"{source.value}:{region}:"
    existing = [key for key in db.dossier_keys_for_root(canonical_url(root_url)) if key.startswith(prefix)]
    if len(existing) == 1:
        return str(existing[0]).removeprefix(prefix)
    if number := normalize_filing_number(title):
        return number
    numbers = {
        n
        for url, _ in attachment_links(page, root_url, root_url)
        if (n := normalize_filing_number(unquote(urlparse(url).path.rsplit("/", 1)[-1])))
    }
    return next(iter(numbers)) if len(numbers) == 1 else "dossier-" + sha256(canonical_url(root_url).encode())[:16]


def collect_dossier(
    http: PoliteHttp,
    db: Database,
    blobs: BlobStore,
    *,
    source: SourceKind,
    region: str,
    number: str,
    root_url: str,
    title: str,
    audit: str | None = None,
    initial_page: str | None = None,
    initial_data: bytes | None = None,
    root_role: DocumentRole | None = None,
    limits: DossierLimits = DEFAULT_DOSSIER_LIMITS,
    min_interval_s: float | None = None,
) -> DossierReport:
    def ingest(data: bytes, url: str, label: str) -> str:
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        with db.transaction():
            record = ingest_pdf(
                db,
                blobs,
                data=data,
                source=source,
                region=region,
                number=number,
                url=url,
                title=title,
                filename=f"{label} {filename}",
                audit=audit,
                role=root_role if canonical_url(url) == canonical_url(root_url) else None,
            )
        return record.id

    return scan_dossier(
        http,
        db,
        filing_key(source, region, number),
        root_url,
        ingest,
        initial_page=initial_page,
        initial_data=initial_data,
        limits=limits,
        min_interval_s=min_interval_s,
    )
