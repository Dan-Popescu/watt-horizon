"""Read-only web server: JSON API + map UI + MCP over streamable HTTP, all backed by `api/queries.py`.

Run:  uv run radar web                  (http://127.0.0.1:8765, MCP at /mcp)
Every endpoint is a sync function (Starlette runs it in a worker thread) with its own short-lived connection.
If `token` is set, every route except / and /healthz requires `Authorization: Bearer <token>`.
"""

import contextlib
import hmac
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from radar.api import exports, queries
from radar.api.review import review_items, submit_review
from radar.classification.gold import GoldLabel
from radar.store.db import Database

STATIC = Path(__file__).parent / "static"
# No project data there; the page asks for the token on the first 401.
PUBLIC_PATHS = frozenset({"/", "/healthz"})
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


def build_app(
    db_path: Path | str,
    *,
    token: str | None = None,
    with_mcp: bool = True,
    mcp_host: str = "127.0.0.1",
    review: tuple[Rebuild, Callable[[], dict[str, GoldLabel]]] | None = None,
) -> Starlette:
    """`mcp_host`: the host clients use; MCP rejects other Host headers (DNS-rebinding protection).
    `review`: enables the internal review endpoints (rebuild function, draft gold labels loader)."""
    db_path = Path(db_path)

    def endpoint(fn: Callable[[Database, Request], Any]) -> Callable[[Request], Response]:
        def handle(request: Request) -> Response:
            if not db_path.exists():
                return _json({"error": f"No database at {db_path}. Run `radar run` first."}, 503)
            try:
                with Database(db_path) as db:
                    result = fn(db, request)
            except ValueError as exc:  # bad category, date or number
                return _json({"error": str(exc)}, 400)
            return result if isinstance(result, Response) else _json(result)

        return handle

    def project(db: Database, request: Request) -> Any:
        detail = queries.get_project(db, request.path_params["project_id"])
        return detail if detail is not None else _json({"error": "unknown project_id"}, 404)

    def projects(db: Database, request: Request) -> Any:
        q = request.query_params
        rows = queries.list_projects(db, category=q.get("category") or None, department=q.get("department") or None)
        min_mw = _optional_float(request, "min_mw")
        return [p for p in rows if min_mw is None or (p.power_mw or 0) >= min_mw]

    def changes(db: Database, request: Request) -> Any:
        q = request.query_params
        return queries.what_changed(
            db, q.get("since", ""), category=q.get("category") or None, department=q.get("department") or None
        )

    def export(db: Database, request: Request) -> Response:
        fmt, q = request.path_params["fmt"], request.query_params
        if fmt not in {"csv", "geojson"}:
            return _json({"error": "format: csv or geojson"}, 404)
        rows = queries.list_projects(db, category=q.get("category") or None, department=q.get("department") or None)
        if fmt == "csv":
            body = "﻿" + exports.to_csv(rows)  # BOM: Excel detects UTF-8
            disposition = {"Content-Disposition": 'attachment; filename="projets.csv"'}
            return Response(body, media_type="text/csv; charset=utf-8", headers={**NO_STORE, **disposition})
        body = json.dumps(exports.to_geojson(rows), ensure_ascii=False)
        disposition = {"Content-Disposition": 'attachment; filename="projets.geojson"'}
        return Response(body, media_type="application/geo+json", headers={**NO_STORE, **disposition})

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

        return await run_in_threadpool(endpoint(write), request)

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

    def index(_: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers=NO_STORE)

    def healthz(_: Request) -> Response:
        return PlainTextResponse("ok")

    routes: list[Route | Mount] = [
        Route("/", index),
        Route("/healthz", healthz),
        Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
        Route("/api/info", endpoint(lambda db, _: queries.dataset_info(db))),
        Route("/api/projects", endpoint(projects)),
        Route("/api/projects/{project_id}", endpoint(project)),
        Route("/api/changes", endpoint(changes)),
        Route("/api/export.{fmt:str}", endpoint(export)),
        Route("/api/review", endpoint(review_list)),
        Route("/api/review/{key}/corrections", review_submit, methods=["POST"]),
        Route("/api/review/corrections/{correction_id:int}", endpoint(review_withdraw), methods=["DELETE"]),
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
