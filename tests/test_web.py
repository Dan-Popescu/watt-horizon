"""Web server: JSON API, exports, token, MCP over streamable HTTP (in-process, no network)."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from radar.store.db import Database
from radar.web.app import build_app

pytestmark = pytest.mark.usefixtures("loaded_db")
LOCAL = "http://127.0.0.1:8765"  # MCP only accepts localhost Host headers when bound locally


@pytest.fixture
def client(loaded_db: Database) -> TestClient:
    return TestClient(build_app(loaded_db.path), base_url=LOCAL)


def test_pages_and_api(client: TestClient) -> None:
    assert "Radar Raccordement" in client.get("/").text
    projects = client.get("/api/projects", params={"category": "bess"}).json()
    assert [p["commune"] for p in projects] == ["Aubenton"]
    assert client.get("/api/projects", params={"min_mw": 500}).json() == []
    detail = client.get(f"/api/projects/{projects[0]['project_id']}").json()
    assert detail["summary"]["power_mw"] == 100.0 and detail["evidence"]
    assert client.get("/api/projects/p_nope").status_code == 404
    assert client.get("/api/projects", params={"category": "unicorn"}).status_code == 400
    assert client.get("/api/changes", params={"since": "2000-01-01"}).json()[0]["kind"] == "new"
    assert client.get("/api/changes", params={"since": "soon"}).status_code == 400


def test_exports(client: TestClient) -> None:
    csv = client.get("/api/export.csv")
    assert csv.headers["content-type"].startswith("text/csv") and csv.content.startswith(b"\xef\xbb\xbf")
    geo = client.get("/api/export.geojson", params={"department": "02"}).json()
    assert [f["properties"]["commune"] for f in geo["features"]] == ["Aubenton"]
    assert client.get("/api/export.xlsx").status_code == 404


def test_token(loaded_db: Database) -> None:
    client = TestClient(build_app(loaded_db.path, token="s3cret", with_mcp=False), base_url=LOCAL)
    assert client.get("/").status_code == 200 and client.get("/healthz").status_code == 200
    assert client.get("/api/info").status_code == 401
    assert client.get("/api/info", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_missing_database(tmp_path: Path) -> None:
    client = TestClient(build_app(tmp_path / "none.sqlite", with_mcp=False), base_url=LOCAL)
    assert client.get("/api/info").status_code == 503


def test_mcp_over_http(loaded_db: Database) -> None:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    with TestClient(build_app(loaded_db.path), base_url=LOCAL) as client:  # `with` runs the MCP lifespan
        response = client.post("/mcp", json=init, headers=headers)
        assert response.status_code == 200, response.text
        assert "radar-raccordement" in response.text
