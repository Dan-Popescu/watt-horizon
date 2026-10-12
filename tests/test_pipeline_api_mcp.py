"""End to end (fixture `loaded_db` in conftest) on generated PDFs: ingest -> process -> link -> projects -> queries -> MCP tools."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp import Client

from radar.api import digest, exports, queries
from radar.classification.gold import GoldLabel
from radar.domain.enums import Category, Confidence, PowerStatus, ReviewDecision
from radar.evaluation.classification import screening_report
from radar.mcp_server import build_server
from radar.pipeline.run import link_and_build
from radar.store.db import _MIGRATIONS, Database


def test_pipeline_builds_traceable_projects(loaded_db: Database) -> None:
    aubenton = loaded_db.filing("dreal_case_by_case:hauts-de-france:010504")
    assert aubenton is not None
    assert aubenton.classification.category is Category.BESS
    assert aubenton.review_decision is ReviewDecision.NOT_SUBJECT
    assert (aubenton.power.mw_max, aubenton.power.mwh, aubenton.power.status) == (100.0, 200.0, PowerStatus.KNOWN)
    assert aubenton.substation == "Les Hoquins" and aubenton.containers == 36
    assert aubenton.location.precision == "exact" and aubenton.location.commune == "Aubenton"
    assert aubenton.confidence is Confidence.HIGH
    assert {"developer", "power", "siret", "review_decision"} <= set(aubenton.evidence)

    bethune = loaded_db.filing("dreal_case_by_case:hauts-de-france:2025-4009")
    assert bethune is not None and bethune.developer == "SIG REVERSE INDUSTRY" and not bethune.unread_scans
    dropped = loaded_db.filing("dreal_case_by_case:hauts-de-france:2025-9999")
    assert dropped is not None and dropped.classification.category is Category.OTHER

    result = screening_report(loaded_db.filings(), {"010504": GoldLabel("010504", Category.BESS)})
    assert result.recall == 1.0


def test_queries(loaded_db: Database) -> None:
    near = queries.search_projects(loaded_db, lat=49.84, lon=4.20, radius_km=10)
    assert [p.name for p in near] == ["Batteries — Aubenton"] and near[0].distance_km is not None
    assert queries.search_projects(loaded_db, category="datacenter")[0].developer == "SIG REVERSE INDUSTRY"
    zone = queries.zone_pressure(loaded_db, 49.84, 4.20, 10)
    assert zone.project_count == 1 and zone.known_power_mw == 100.0
    assert queries.projects_near_substation(loaded_db, "hoquins")[0].commune == "Aubenton"
    detail = queries.get_project(loaded_db, near[0].project_id)
    assert detail is not None
    assert any(e.field == "power" and e.document_url for e in detail.evidence)
    assert any(r.key == "company:999" for r in detail.related)  # same developer word « apal »
    with pytest.raises(ValueError):
        queries.search_projects(loaded_db, category="unicorn")


@pytest.mark.anyio
async def test_mcp_tools(loaded_db: Database) -> None:
    server = build_server(loaded_db.path)
    async with Client(server, raise_exceptions=True) as client:
        info = await client.call_tool("dataset_info", {})
        assert info.structured_content is not None and info.structured_content["counts"]["projects"] == 2
        found = await client.call_tool("search_projects", {"category": "bess"})
        assert found.structured_content is not None
        project_id = found.structured_content["result"][0]["project_id"]
        detail = await client.call_tool("get_project", {"project_id": project_id})
        assert detail.structured_content is not None and detail.structured_content["summary"]["power_mw"] == 100.0
        missing = await client.call_tool("get_project", {"project_id": "p_nope"})
        assert missing.is_error


def test_change_log_is_quiet_on_rebuild_and_records_real_changes(loaded_db: Database) -> None:
    assert link_and_build(loaded_db)["changes"] == {}  # same inputs -> nothing logged
    baseline = queries.what_changed(loaded_db, "2000-01-01")
    assert {c.kind for c in baseline} == {"new"} and len(baseline) == 2

    key = "dreal_case_by_case:hauts-de-france:010504"
    filing = loaded_db.filing(key)
    assert filing is not None
    bigger = filing.power.model_copy(update={"mw_max": 120.0})
    with loaded_db.transaction():
        loaded_db.upsert_filing(filing.model_copy(update={"power": bigger}))
    later = datetime(2030, 1, 1, tzinfo=UTC)
    assert link_and_build(loaded_db, now=later)["changes"] == {"changed": 1}

    [change] = queries.what_changed(loaded_db, "2030-01-01")
    assert change.kind == "changed" and "puissance (MW) 100 → 120" in change.summary
    assert [f.field for f in change.fields] == ["power_mw"]
    project = loaded_db.project(change.project_id)
    assert project is not None and project.updated_at == later
    with pytest.raises(ValueError):
        queries.what_changed(loaded_db, "last week")


def test_exports(loaded_db: Database) -> None:
    projects = queries.list_projects(loaded_db)
    rows = exports.to_csv(projects).splitlines()
    assert rows[0].startswith("project_id;name;category") and len(rows) == 3
    assert "distance_km" not in rows[0] and any(";100;" in r for r in rows)  # 100.0 MW rendered for Excel
    geo = exports.to_geojson(projects)
    aubenton = next(f for f in geo["features"] if f["properties"]["commune"] == "Aubenton")
    lon, lat = aubenton["geometry"]["coordinates"]
    assert 4.0 < lon < 4.3 and 49.7 < lat < 49.9  # GeoJSON order is [lon, lat]


@pytest.mark.anyio
async def test_mcp_what_changed(loaded_db: Database) -> None:
    async with Client(build_server(loaded_db.path), raise_exceptions=True) as client:
        result = await client.call_tool("what_changed", {"since": "2000-01-01", "category": "bess"})
        assert result.structured_content is not None
        assert [c["kind"] for c in result.structured_content["result"]] == ["new"]
        bad = await client.call_tool("what_changed", {"since": "yesterday"})
        assert bad.is_error


def test_migrates_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(_MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with Database(path) as db:
        assert db.counts()["project_changes"] == 0


def test_digest(loaded_db: Database) -> None:
    d = digest.build_digest(
        loaded_db, "2025-01-01", scope="Test", map_url="https://radar.example/", today=date(2026, 1, 9)
    )
    assert [p.summary.commune for p in d.new_projects] == ["Aubenton", "Béthune"]  # newest filing first
    assert d.changes == []  # baseline "new" log entries are not repeated
    html = digest.render_html(d)
    assert "100 MW / 200 MWh" in html and "https://radar.example/#p=" in html and "Document source" in html
    assert "2 nouveaux dossiers · 100 MW déclarés" in digest.render_text(d)
    empty = digest.build_digest(loaded_db, "2030-01-01")
    assert "Aucun nouveau dossier" in digest.render_html(empty)
