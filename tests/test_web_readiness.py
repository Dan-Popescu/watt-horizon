"""Readiness observes the existing database; paginated HTTP reads remain bounded."""

import sqlite3
from pathlib import Path

import pytest
from mcp import Client
from starlette.testclient import TestClient

from radar.mcp_server import build_server
from radar.store.db import Database
from radar.web.app import build_app


def test_readiness_does_not_create_or_migrate_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "radar.sqlite"
    client = TestClient(build_app(path, with_mcp=False))
    assert client.get("/healthz").status_code == 503
    assert not path.exists() and not path.parent.exists()
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=1")
    assert client.get("/healthz").status_code == 503
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_ready_database_serves_pages_and_keeps_legacy_list(loaded_db: Database) -> None:
    client = TestClient(build_app(loaded_db.path, with_mcp=False))
    assert client.get("/healthz").status_code == 200
    first = client.get("/api/projects/page?limit=1").json()
    assert first["total"] == 2 and len(first["items"]) == 1
    assert first["next_cursor"]
    second = client.get("/api/projects/page", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["next_cursor"] is None
    assert second["items"][0]["project_id"] != first["items"][0]["project_id"]
    assert isinstance(client.get("/api/projects").json(), list)
    assert client.get("/api/projects/page?bbox=bad").status_code == 400


def test_legacy_project_list_is_bounded_and_links_to_next_page(loaded_db: Database) -> None:
    seed = loaded_db.projects()[0]
    with loaded_db.transaction():
        loaded_db.replace_projects([seed.model_copy(update={"id": f"synthetic:{i:04}"}) for i in range(201)])
    client = TestClient(build_app(loaded_db.path, with_mcp=False))
    first = client.get("/api/projects")
    assert len(first.json()) == 200
    assert first.headers["X-Total-Count"] == "201"
    second = client.get("/api/projects", params={"cursor": first.headers["X-Next-Cursor"]})
    assert len(second.json()) == 1 and "X-Next-Cursor" not in second.headers
    assert second.json()[0]["project_id"] not in {row["project_id"] for row in first.json()}


def test_read_only_database_refuses_writes_without_changing_data(loaded_db: Database) -> None:
    with Database(loaded_db.path, read_only=True) as reader:
        assert reader.counts()["projects"] == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"), reader.transaction():
            reader.replace_projects([])
    assert loaded_db.counts()["projects"] == 2


def test_partial_review_exposes_fields_still_awaiting_approval(loaded_db: Database) -> None:
    from radar.api.review import review_items, submit_review

    key = "dreal_case_by_case:hauts-de-france:010504"
    submit_review(loaded_db, key, author="Synthetic reviewer", changes={"power_mw": 90})
    item = next(row for row in review_items(loaded_db, {}) if row.key == key)
    assert "power_mw" not in item.pending_fields
    assert "developer" in item.pending_fields


def test_server_switches_published_dataset_once_per_request(loaded_db: Database, tmp_path: Path) -> None:
    second = tmp_path / "next" / "radar.sqlite"
    with Database(second):
        pass
    selected = [loaded_db.path]
    calls = []

    def resolve() -> Path:
        calls.append(selected[0])
        return selected[0]

    client = TestClient(build_app(resolve, with_mcp=False))
    assert client.get("/api/projects/page").json()["total"] == 2
    assert len(calls) == 1
    selected[0] = second
    assert client.get("/api/projects/page").json()["total"] == 0
    assert len(calls) == 2


@pytest.mark.anyio
async def test_mcp_page_follows_dataset_resolver(loaded_db: Database, tmp_path: Path) -> None:
    second = tmp_path / "next-mcp.sqlite"
    with Database(second):
        pass
    selected = [loaded_db.path]
    async with Client(build_server(lambda: selected[0]), raise_exceptions=True) as client:
        first = await client.call_tool("project_page", {"limit": 1})
        assert first.structured_content is not None
        assert first.structured_content["total"] == 2 and len(first.structured_content["items"]) == 1
        second_page = await client.call_tool(
            "project_page", {"limit": 1, "cursor": first.structured_content["next_cursor"]}
        )
        assert second_page.structured_content is not None
        assert (
            second_page.structured_content["items"][0]["project_id"]
            != first.structured_content["items"][0]["project_id"]
        )
        selected[0] = second
        empty = await client.call_tool("project_page", {})
        assert empty.structured_content is not None and empty.structured_content["total"] == 0
