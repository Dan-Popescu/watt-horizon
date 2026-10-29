"""Read-only web server: JSON API + map UI + MCP over streamable HTTP, all backed by `api/queries.py`.

Run:  uv run radar web                  (http://127.0.0.1:8765, MCP at /mcp)
Every endpoint is a sync function (Starlette runs it in a worker thread) with its own short-lived connection.
If `token` is set, every route except / and /healthz requires `Authorization: Bearer <token>`.
"""

import contextlib
import hmac
import sqlite3
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

import anyio
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from radar.api import exports, queries
from radar.api.review import link_items, review_items, submit_link_decision, submit_review
from radar.api.views import ProjectPage
from radar.classification.gold import GoldLabel
from radar.domain.enums import TARGET_CATEGORIES, Category
from radar.pipeline.operations import ProcessBusyError, operational_status, process_lock
from radar.pipeline.run import link_and_build
from radar.quality import pageview, propagate
from radar.store.db import Database

STATIC = Path(__file__).parent / "static"
# No project data there; the page asks for the token on the first 401.
PUBLIC_PATHS = frozenset({"/", "/healthz", "/livez"})
PUBLIC_PREFIX = "/static/"
NO_STORE = {"Cache-Control": "no-store"}


def _json(payload: Any, status: int = 200) -> JSONResponse:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    elif isinstance(payload, list):
        payload = [p.model_dump(mode="json") if isinstance(p, BaseModel) else p for p in payload]
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _optional_float(request: Request, name: str) -> float | None:
    raw = request.query_params.get(name)
    return float(raw) if raw not in (None, "") else None


class BearerToken(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self.expected = f"Bearer {token}".encode()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith(PUBLIC_PREFIX):
            return await call_next(request)
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, self.expected):
            return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


Rebuild = Callable[[Database, str], None]  # re-process one filing (applies corrections) and rebuild projects


class _ExportStream(StreamingResponse):
    """Close the suspended database generator even if ASGI send fails or the client disconnects."""

    def __init__(self, stream: AsyncGenerator[str, None], *, media_type: str, headers: dict[str, str]) -> None:
        self.stream = stream
        super().__init__(stream, media_type=media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.stream.aclose()


def build_app(
    db_path: Path | str | Callable[[], Path],
    *,
    token: str | None = None,
    with_mcp: bool = True,
    mcp_host: str = "127.0.0.1",
    review: tuple[Rebuild, Callable[[], dict[str, GoldLabel]]] | None = None,
    quality: Callable[[Database], dict[str, Any]] | None = None,
    document: Callable[[str], bytes] | None = None,
    evidence_limits: dict[str, int] | None = None,
) -> Starlette:
    """`mcp_host`: the host clients use; MCP rejects other Host headers (DNS-rebinding protection).
    `review`: enables the internal review endpoints (rebuild function, draft gold labels loader).
    `quality`: enables the internal quality dashboard endpoint (/api/quality).
    `document`: PDF bytes by document id; with `review`, enables the highlighted evidence page (review only)."""

    def dataset_path() -> Path:
        return Path(db_path() if callable(db_path) else db_path)

    evidence_slots = BoundedSemaphore(1)

    def endpoint(fn: Callable[[Database, Request], Any], *, write: bool = False) -> Callable[[Request], Response]:
        def handle(request: Request) -> Response:
            try:
                selected_path = dataset_path()
                if not selected_path.exists():
                    return _json({"error": "Base absente : lancez radar run avant de servir les données"}, 503)
                with (
                    process_lock(selected_path.parent) if write else contextlib.nullcontext(),
                    Database(selected_path, read_only=not write) as db,
                ):
                    result = fn(db, request)
            except ProcessBusyError:
                return _json({"error": "Traitement en cours, réessayez dans quelques instants"}, 503)
            except (sqlite3.Error, OSError):
                return _json({"error": "Base temporairement indisponible"}, 503)
            except ValueError as exc:  # bad category, date or number
                return _json({"error": str(exc)}, 400)
            return result if isinstance(result, Response) else _json(result)

        return handle

    def project(db: Database, request: Request) -> Any:
        detail = queries.get_project(db, request.path_params["project_id"])
        return detail if detail is not None else _json({"error": "unknown project_id"}, 404)

    def projects(db: Database, request: Request) -> Any:
        page = project_page(db, request)
        response = _json(page.items)
        response.headers["X-Total-Count"] = str(page.total)
        if page.next_cursor:
            response.headers["X-Next-Cursor"] = page.next_cursor
            response.headers["Link"] = f'<{request.url.include_query_params(cursor=page.next_cursor)}>; rel="next"'
        return response

    def project_page(db: Database, request: Request) -> ProjectPage:
        q = request.query_params
        bounds = None
        if q.get("bbox"):
            values = tuple(float(value) for value in q["bbox"].split(","))
            if len(values) != 4:
                raise ValueError("Zone géographique invalide")
            bounds = (values[0], values[1], values[2], values[3])
        return queries.project_page(
            db,
            category=q.get("category") or None,
            categories=q["categories"].split(",") if q.get("categories") else ([] if "categories" in q else None),
            department=q.get("department") or None,
            commune=q.get("commune") or None,
            lat=_optional_float(request, "lat"),
            lon=_optional_float(request, "lon"),
            radius_km=float(q.get("radius_km", "20")),
            min_mw=_optional_float(request, "min_mw"),
            since=q.get("since") or None,
            query=q.get("q") or None,
            bounds=bounds,
            limit=int(q.get("limit", "200")),
            cursor=q.get("cursor") or None,
        )

    def changes(db: Database, request: Request) -> Any:
        q = request.query_params
        return queries.what_changed(
            db, q.get("since", ""), category=q.get("category") or None, department=q.get("department") or None
        )

    def export(db: Database, request: Request) -> Response:
        fmt, q = request.path_params["fmt"], request.query_params
        if fmt not in {"csv", "geojson"}:
            return _json({"error": "format: csv or geojson"}, 404)
        requested = q.get("category")
        categories = [Category(requested).value] if requested else [c.value for c in TARGET_CATEGORIES]
        selected_path, department = db.path, q.get("department") or None

        async def content() -> AsyncGenerator[str, None]:
            # The endpoint connection closes before StreamingResponse consumes this iterator.
            # Keep this new connection and cursor on the event-loop thread throughout the stream.
            with Database(selected_path, read_only=True) as reader:
                projects = reader.iter_projects(categories=categories, department=department)
                chunks = exports.iter_csv(projects) if fmt == "csv" else exports.iter_geojson(projects)
                try:
                    if fmt == "csv":
                        yield "\ufeff"  # Excel detects UTF-8 from the BOM.
                    yield next(chunks)  # The header/prefix is available before consuming any project.
                    pending: list[str] = []
                    size = 0
                    for chunk in chunks:
                        pending.append(chunk)
                        size += len(chunk)
                        if len(pending) >= 64 or size >= 65536:
                            yield "".join(pending)
                            pending.clear()
                            size = 0
                    if pending:
                        yield "".join(pending)
                finally:
                    chunks.close()

        media_type = "text/csv; charset=utf-8" if fmt == "csv" else "application/geo+json"
        disposition = {"Content-Disposition": f'attachment; filename="projets.{fmt}"'}
        return _ExportStream(content(), media_type=media_type, headers={**NO_STORE, **disposition})

    def review_list(db: Database, _: Request) -> Any:
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        return review_items(db, review[1]())

    async def review_submit(request: Request) -> Response:
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "JSON body expected"}, 400)
        key = request.path_params["key"]

        def write(db: Database, _: Request) -> Any:
            submit_review(
                db,
                key,
                author=str(body.get("author", "")),
                changes=dict(body.get("changes") or {}),
                note=str(body.get("note", "")),
                validate=bool(body.get("validate", False)),
                cause=body.get("cause") or None,
                blind=bool(body.get("blind", False)),
            )
            review[0](db, key)
            return next(i for i in review_items(db, review[1]()) if i.key == key)

        return await run_in_threadpool(endpoint(write, write=True), request)

    def review_links(db: Database, _: Request) -> Any:
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        return link_items(db)

    async def review_link_decide(request: Request) -> Response:
        """Body: {a, b, verdict, author, note}. Projects are rebuilt at once (the decision overrides grouping)."""
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "JSON body expected"}, 400)

        def write(db: Database, _: Request) -> Any:
            decision = submit_link_decision(
                db,
                str(body.get("a", "")),
                str(body.get("b", "")),
                verdict=str(body.get("verdict", "")),
                author=str(body.get("author", "")),
                note=str(body.get("note", "")),
            )
            link_and_build(db)
            return {"a": decision.a, "b": decision.b, "verdict": decision.verdict}

        return await run_in_threadpool(endpoint(write, write=True), request)

    def review_suggestions(db: Database, _: Request) -> Any:
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        return [propagate.as_dict(x) for x in propagate.suggestions(db.filings(), db.corrections())]

    async def review_propagate(request: Request) -> Response:
        """Apply one suggestion (field = value) to the listed filings, each recorded as its own correction."""
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        try:
            body = await request.json()
        except ValueError:
            return _json({"error": "JSON body expected"}, 400)

        def write(db: Database, _: Request) -> Any:
            keys = [str(k) for k in body.get("keys") or []]
            note = f"propagée depuis {body.get('learned_from', '?')}"
            for key in keys:
                submit_review(
                    db,
                    key,
                    author=str(body.get("author", "")),
                    changes={str(body["field"]): body.get("value")},
                    note=note,
                    cause=body.get("cause") or None,
                )
            for key in keys:
                review[0](db, key)
            return {"applied": len(keys)}

        return await run_in_threadpool(endpoint(write, write=True), request)

    def review_withdraw(db: Database, request: Request) -> Any:
        if review is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        correction_id = int(request.path_params["correction_id"])
        key = next((c.filing_key for c in db.corrections() if c.id == correction_id), None)
        with db.transaction():
            if key is None or not db.withdraw_correction(correction_id):
                return _json({"error": "unknown correction"}, 404)
        review[0](db, key)
        return {"withdrawn": correction_id}

    def evidence_page(db: Database, request: Request) -> Any:
        """The PDF page a value was read from, passage highlighted, person names blacked out (never stored)."""
        if review is None or document is None:
            return _json({"error": "review mode is off (radar web --review)"}, 404)
        filing = db.filing(request.path_params["key"])
        ev = filing.evidence.get(request.path_params["field"]) if filing else None
        if ev is None or ev.document_id == "human":
            return _json({"error": "no document evidence for this field"}, 404)
        busy = JSONResponse(
            {"error": "Un aperçu PDF est déjà en cours. Réessayez dans un instant."},
            status_code=503,
            headers={**NO_STORE, "Retry-After": "1"},
        )
        if not evidence_slots.acquire(blocking=False):
            return busy
        try:
            data = document(ev.document_id)
            view = pageview.render(data, ev.quote, ev.locator, **(evidence_limits or {}))
        except FileNotFoundError:
            return _json({"error": "document file missing (blobs)"}, 404)
        except pageview.PageViewBusyError:
            return busy
        finally:
            evidence_slots.release()
        headers = {
            **NO_STORE,
            "X-Radar-Page": str(view.page),
            "X-Radar-Pages": str(view.pages),
            "X-Radar-Found": "1" if view.found else "0",
            "X-Radar-Top": f"{view.top:.3f}",
        }
        return Response(view.png, media_type="image/png", headers=headers)

    def quality_view(db: Database, _: Request) -> Any:
        if quality is None:
            return _json({"error": "quality dashboard is off (radar web --review)"}, 404)
        return quality(db)

    def index(_: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers=NO_STORE)

    def healthz(_: Request) -> Response:
        try:
            with Database(dataset_path(), read_only=True) as db:
                db.conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
        except (sqlite3.Error, ValueError, OSError):
            return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ok")

    routes: list[Route | Mount] = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/livez", lambda _: PlainTextResponse("ok")),
        Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        Route("/api/info", endpoint(lambda db, _: queries.dataset_info(db))),
        Route("/api/status", endpoint(lambda db, _request: operational_status(db.path.parent))),
        Route("/api/projects", endpoint(projects)),
        Route("/api/projects/page", endpoint(project_page)),
        Route("/api/projects/{project_id}", endpoint(project)),
        Route("/api/changes", endpoint(changes)),
        Route("/api/export.{fmt:str}", endpoint(export)),
        Route("/api/quality", endpoint(quality_view)),
        Route("/api/review", endpoint(review_list)),
        Route("/api/review/{key}/corrections", review_submit, methods=["POST"]),
        Route("/api/review/links", endpoint(review_links)),
        Route("/api/review/links", review_link_decide, methods=["POST"]),
        Route("/api/review/suggestions", endpoint(review_suggestions)),
        Route("/api/review/suggestions/apply", review_propagate, methods=["POST"]),
        Route("/api/review/{key}/evidence/{field}.png", endpoint(evidence_page)),
        Route("/api/review/corrections/{correction_id:int}", endpoint(review_withdraw, write=True), methods=["DELETE"]),
    ]
    lifespan = None
    if with_mcp:
        from radar.mcp_server import build_server  # deferred: keeps the web module importable on its own

        # Stateless: any worker can answer any request (no sticky sessions needed behind a load balancer).
        mcp_app = build_server(db_path).streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, host=mcp_host
        )
        routes.append(Mount("", app=mcp_app))  # last: serves /mcp only, everything else 404s there

        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            async with mcp_app.router.lifespan_context(mcp_app):  # starts the MCP session manager
                yield

    middleware = [Middleware(BearerToken, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
