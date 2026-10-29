"""Offline CLI wiring and a single bounded mocked official-source qualification."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from radar import cli, documentary_cli
from radar.config import Settings
from radar.net.http import PoliteHttp
from radar.sources.catalog import load_catalog, save_catalog
from radar.store.db import Database


def test_search_plan_preview_enqueue_and_retry_are_safe(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    args = ["discover", "plan", "--term", "stockage batteries", "--max-queries", "2"]
    assert cli.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["network_requests"] == 0 and len(preview["queries"]) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_queries").fetchone()[0] == 0
    assert cli.main([*args, "--enqueue"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 1
    assert cli.main([*args, "--enqueue"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 0
    assert cli.main(["discover", "search"]) == 2
    assert "RADAR_OFFLINE" in json.loads(capsys.readouterr().out)["error"]


def test_offline_quality_loop_preserves_unknown_quality_and_cost(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["quality-loop"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["quality_measured"] is False
    assert "reviewer" not in report


def test_catalog_adoption_is_visible_to_coverage_and_collection_region_lookup(
    settings: Settings,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Synthetic observed source configuration, not a declaration of real website coverage.
    path = settings.catalog_path
    current = load_catalog(path)
    region = current.regions[0]
    listing = "examen-archive-r7.html"
    extended = region.model_copy(
        update={"dreal_listings": (*region.dreal_listings, listing), "dreal_periods": {listing: "2024"}}
    )
    save_catalog(path, current.model_copy(update={"regions": (extended,)}))
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["coverage", "--years", "2024"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert any(row["scope"] == listing and row["period"] == "2024" for row in report["scopes"])
    assert cli.main(["catalog", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["regions"][0]["dreal_periods"][listing] == "2024"


def test_online_search_cli_uses_physical_budget_and_persists_only_leads(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    current = settings
    monkeypatch.setattr(cli, "load_settings", lambda: current)
    assert cli.main(["discover", "plan", "--term", "stockage batteries", "--enqueue"]) == 0
    capsys.readouterr()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            text='<div class="result"><a class="result__a" '
            'href="https://www.nord.gouv.fr/dossier-stockage">Dossier officiel de stockage</a></div>',
        )

    current = replace(settings, offline=False)
    monkeypatch.setattr(
        documentary_cli,
        "make_http",
        lambda _: PoliteHttp("test", default_interval_s=0, transport=httpx.MockTransport(handler)),
    )
    assert cli.main(["discover", "search", "--max-queries", "1", "--max-requests", "1", "--interval", "0"]) == 0
    json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and db.filing_keys_with_documents() == []
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_leads").fetchone()[0] == 1
