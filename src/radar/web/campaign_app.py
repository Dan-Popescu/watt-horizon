"""A private, fixed-identity blind annotation app, separate from the public read API."""

import hashlib
import hmac
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from pydantic import Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from radar.config import Settings
from radar.domain.privacy import PersonScrubber, is_person_field
from radar.evaluation.campaign import Annotation, FrozenModel
from radar.quality import pageview
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore
from radar.web.campaign_session import ReviewConflictError, ReviewSession
from radar.wiring import make_services

STATIC = Path(__file__).parent / "static" / "campaign.html"
NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}
MAX_BODY_BYTES = 256 * 1024


def _table_cells(pdf: PdfContent, page: int, scrubber: PersonScrubber) -> tuple[list[dict[str, str]], bool]:
    cells: list[dict[str, str]] = []
    characters = 0
    truncated = False
    for table in pdf.tables:
        if table.page != page:
            continue
        for row_index, row in enumerate(table.rows, start=1):
            for column_index, value in enumerate(row, start=1):
                safe = scrubber.scrub(value) or ""
                if len(cells) >= 200 or characters + len(safe[:400]) > 20_000:
                    return cells, True
                truncated |= len(safe) > 400
                cells.append(
                    {"locator": f"table:{table.index}:row:{row_index}:column:{column_index}", "text": safe[:400]}
                )
                characters += len(safe[:400])
    return cells, truncated


class SaveRequest(FrozenModel):
    revision: int = Field(ge=0)
    annotation: Annotation
    finalize: bool = False
    duration_s: float = Field(default=0, ge=0, le=14_400)


class ReviewAccess(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str | None) -> None:
        super().__init__(app)
        self.expected = f"Bearer {token}".encode() if token else None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            request.url.path not in {"/", "/healthz"}
            and self.expected
            and not hmac.compare_digest(request.headers.get("authorization", "").encode(), self.expected)
        ):
            return PlainTextResponse("Authentification requise.", 401, headers=NO_STORE)
        if request.method == "POST" and (
            request.headers.get("x-campaign-review") != "1"
            or request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json"
        ):
            return PlainTextResponse("Requête de relecture invalide.", 403, headers=NO_STORE)
        response = await call_next(request)
        response.headers.update(NO_STORE)
        return response


def build_app(
    settings: Settings,
    directory: Path,
    reviewer: str,
    slot: Literal["a", "b"],
    *,
    host: str = "127.0.0.1",
    token: str | None = None,
    public_host: str | None = None,
) -> Starlette:
    """The caller binds ``host``; non-local hosts need a token and an explicit browser host."""
    token = token or settings.api_token
    local = host in {"127.0.0.1", "localhost", "::1"}
    if not local and (not token or not public_host):
        raise ValueError("Une exposition distante exige un jeton et un nom d'hôte public explicite.")
    session = ReviewSession(directory, reviewer, slot)
    blobs = BlobStore(settings.blobs_dir)
    services = make_services(settings, None, llm="cached")
    read_slot = Lock()

    def read(document_id: str, data: bytes) -> PdfContent:
        with read_slot:
            return services.ocr.complete(document_id, data, services.reader.read(document_id, data))

    def raw(document_id: str) -> bytes:
        data = blobs.get(document_id, max_bytes=settings.pdf_max_bytes)
        if hashlib.sha256(data).hexdigest() != document_id:
            raise ValueError("Le document ne correspond plus à son empreinte.")
        return data

    def document(request: Request) -> tuple[str, int]:
        key = request.query_params.get("filing_key", "")
        record = session.record(key)
        document_id = request.path_params["document_id"]
        descriptor = next((d for d in record.documents if d.id == document_id), None)
        if descriptor is None:
            raise KeyError("Cette pièce n'appartient pas au dossier attribué.")
        page = int(request.path_params["page"])
        if not 1 <= page <= descriptor.pages:
            raise ValueError("Page hors du document.")
        return document_id, page

    def segments(request: Request) -> Response:
        try:
            record = session.record(request.query_params.get("filing_key", ""))
            document_id = request.path_params["document_id"]
            if not any(item.id == document_id for item in record.documents):
                raise KeyError("Cette pièce n'appartient pas au dossier attribué.")
            offset = int(request.query_params.get("offset", "0"))
            if offset < 0:
                raise ValueError("Position de lecture invalide.")
            content = read(document_id, raw(document_id))
            scrubber = PersonScrubber.for_filing([content.fields], [])
            selected = content.segments[offset : offset + 200]
            rows: list[dict[str, Any]] = []
            characters = 0
            for segment in selected:
                values = {
                    "locator": segment.locator,
                    "text": scrubber.scrub(segment.text) or "",
                    "header": scrubber.scrub(segment.header) or "",
                    "context": scrubber.scrub(segment.context) or "",
                    "uncertain": segment.uncertain,
                    "reason": segment.reason,
                }
                size = sum(len(value) for value in values.values() if isinstance(value, str))
                if rows and characters + size > 50_000:
                    break
                if size > 50_000:
                    values["text"] = str(values["text"])[:20_000]
                    values["context"] = str(values["context"])[:20_000]
                    values["header"] = str(values["header"])[:5000]
                    values["preview_truncated"] = True
                rows.append(values)
                characters += size
            next_offset = offset + len(rows)
            return JSONResponse(
                {
                    "format": content.format,
                    "segments": rows,
                    "errors": content.format_errors,
                    "next_offset": next_offset if next_offset < len(content.segments) else None,
                    "total_segments": len(content.segments),
                },
                headers=NO_STORE,
            )
        except (ValueError, KeyError, OSError) as exc:
            return error(exc)

    def error(exc: Exception) -> JSONResponse:
        status = 409 if isinstance(exc, ReviewConflictError) else 404 if isinstance(exc, KeyError) else 400
        return JSONResponse({"error": str(exc)}, status, headers=NO_STORE)

    def home(_: Request) -> Response:
        return FileResponse(STATIC, headers=NO_STORE)

    def queue(_: Request) -> Response:
        try:
            return JSONResponse(session.queue(), headers=NO_STORE)
        except (ValueError, OSError) as exc:
            return error(exc)

    def record(request: Request) -> Response:
        try:
            return JSONResponse(session.load(request.query_params.get("filing_key", "")), headers=NO_STORE)
        except (ValueError, KeyError, OSError) as exc:
            return error(exc)

    async def save(request: Request) -> Response:
        try:
            content = bytearray()
            async for block in request.stream():
                if len(content) + len(block) > MAX_BODY_BYTES:
                    return JSONResponse({"error": "Fiche trop volumineuse."}, 413, headers=NO_STORE)
                content.extend(block)
            body = SaveRequest.model_validate_json(content)
            result = await run_in_threadpool(
                session.save,
                body.annotation.filing_key,
                body.annotation,
                body.revision,
                body.finalize,
                body.duration_s,
                blobs,
                read,
            )
            return JSONResponse(result, headers=NO_STORE)
        except (ValueError, KeyError, OSError) as exc:
            return error(exc)

    def image(request: Request) -> Response:
        try:
            document_id, page = document(request)
            rendered = pageview.render(
                raw(document_id),
                "",
                f"page:{page}",
                max_bytes=settings.pdf_max_bytes,
                max_pages=settings.pdf_max_pages,
                document_timeout_s=min(settings.pdf_timeout_s, 15),
            )
            return Response(rendered.png, media_type="image/png", headers=NO_STORE)
        except pageview.PageViewBusyError:
            return JSONResponse(
                {"error": "Le rendu est occupé ; réessayez."}, 503, headers=NO_STORE | {"Retry-After": "1"}
            )
        except (ValueError, KeyError, OSError) as exc:
            return error(exc)

    def text(request: Request) -> Response:
        try:
            document_id, page = document(request)
            pdf = read(document_id, raw(document_id))
            scrubber = PersonScrubber.for_filing([pdf.fields], [])
            source = pdf.ocr_texts.get(page) or pdf.page_texts[page - 1]
            safe_text = scrubber.scrub(source) or ""
            safe_fields = {
                name: scrubber.scrub(value) for name, value in pdf.fields.items() if not is_person_field(name)
            }
            fields = {name: (value or "")[:4000] for name, value in list(safe_fields.items())[:100] if len(name) <= 200}
            cells, tables_truncated = _table_cells(pdf, page, scrubber)
            return JSONResponse(
                {
                    "page": page,
                    "text": safe_text[:50_000],
                    "unread": page in pdf.unread_scanned_pages,
                    "truncated": len(safe_text) > 50_000
                    or len(safe_fields) > len(fields)
                    or any(len(value or "") > 4000 for value in safe_fields.values())
                    or tables_truncated,
                    "table_incomplete": page in pdf.table_errors,
                    "fields": fields,
                    "tables": cells,
                },
                headers=NO_STORE,
            )
        except (ValueError, KeyError, OSError) as exc:
            return error(exc)

    app = Starlette(
        routes=[
            Route("/", home),
            Route("/healthz", lambda _: PlainTextResponse("ok", headers=NO_STORE)),
            Route("/api/queue", queue),
            Route("/api/record", record),
            Route("/api/save", save, methods=["POST"]),
            Route("/api/documents/{document_id}/pages/{page:int}.png", image),
            Route("/api/documents/{document_id}/pages/{page:int}/text", text),
            Route("/api/documents/{document_id}/segments", segments),
        ],
        middleware=[
            Middleware(
                TrustedHostMiddleware,
                allowed_hosts=[public_host] if public_host else ["127.0.0.1", "localhost", "[::1]"],
            ),
            Middleware(ReviewAccess, token=token),
        ],
    )
    app.state.review_session = session
    return app
