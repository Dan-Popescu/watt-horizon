"""Exports emit bounded chunks and release their sources when a consumer stops."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient
from starlette.types import Message

from radar.api import exports
from radar.domain.enums import Category, Confidence
from radar.domain.models import Location, Power, Project
from radar.store.db import Database
from radar.web.app import build_app


@pytest.fixture
def export_db(db: Database) -> Database:
    rows = [
        Project(
            id=f"synthetic:{index}",
            name="Énergie; synthetic export",
            category=Category.BESS,
            developer=None,
            location=Location(),
            power=Power(mw_max=1) if index == 0 else Power(),
            substation=None,
            status="Synthetic",
            first_seen=None,
            updated_at=datetime(2026, 10, 29, tzinfo=UTC),
            confidence=Confidence.REVIEW,
            filing_keys=(f"synthetic:{index}",),
        )
        for index in range(2)
    ]
    with db.transaction():
        db.replace_projects(rows)
    return db


@pytest.mark.parametrize("format_name", ["csv", "geojson"])
def test_export_generators_are_lazy_and_close_their_source(export_db: Database, format_name: str) -> None:
    consumed: list[str] = []
    closed: list[bool] = []

    def records() -> Iterator[Project]:
        try:
            for project in export_db.iter_projects():
                consumed.append(project.id)
                yield project
        finally:
            closed.append(True)

    chunks = exports.iter_csv(records()) if format_name == "csv" else exports.iter_geojson(records())
    assert next(chunks)
    assert consumed == []
    assert next(chunks)
    assert len(consumed) == 1
    chunks.close()
    assert closed == [True]


def test_streaming_http_exports_preserve_headers_and_complete_formats(export_db: Database) -> None:
    resolved: list[Path] = []

    def resolve() -> Path:
        resolved.append(export_db.path)
        return export_db.path

    client = TestClient(build_app(resolve, with_mcp=False))
    csv = client.get("/api/export.csv")
    assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf")
    assert csv.headers["content-disposition"] == 'attachment; filename="projets.csv"'
    assert len(csv.text.splitlines()) == 3
    assert len(resolved) == 1
    geo = client.get("/api/export.geojson")
    assert geo.headers["content-type"].startswith("application/geo+json")
    assert geo.headers["content-disposition"] == 'attachment; filename="projets.geojson"'
    assert len(json.loads(geo.content)["features"]) == 2
    assert len(resolved) == 2


@pytest.mark.anyio
async def test_disconnected_export_releases_sqlite_read_snapshot(export_db: Database) -> None:
    app = build_app(export_db.path, with_mcp=False)
    chunks = 0

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal chunks
        if message["type"] == "http.response.body" and message.get("body"):
            chunks += 1
            if chunks == 3:
                raise OSError("synthetic client disconnect")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/export.csv",
        "raw_path": b"/api/export.csv",
        "query_string": b"",
        "headers": [],
        "server": ("localhost", 80),
        "client": ("localhost", 1234),
        "root_path": "",
    }
    with pytest.raises(ClientDisconnect):
        await app(scope, receive, send)
    assert export_db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
