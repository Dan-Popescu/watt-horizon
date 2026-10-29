"""Coverage evidence and page-boundary resumability on mocked public-site layouts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources.coverage import inventory
from radar.sources.dreal import collect_dreal
from radar.sources.mrae import collect_mrae
from radar.sources.regions import get_region
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import FILLER, make_pdf

REGION = replace(get_region("hauts-de-france"), dreal_base="https://example.test/", dreal_listings=("2026-r1.html",))


def listing(offset: int) -> str:
    return (
        f'<a href="stockage-012667-a{offset + 1}.html">Projet de centrale de stockage d’énergie par batteries 012667</a>'
        '<a href="?debut_listearticles=16">Dernière page</a>'
    )


def test_backfill_resumes_and_recent_cannot_overwrite_history(tmp_path: Path) -> None:
    requests: list[str] = []
    pdf = make_pdf([FILLER])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf)
        if "stockage-" in request.url.path:
            return httpx.Response(200, text='<a href="012667-cerfa.pdf">Formulaire</a>')
        return httpx.Response(200, text=listing(int(request.url.params.get("debut_listearticles", 0))))

    with (
        Database(tmp_path / "state.sqlite") as db,
        PoliteHttp("test", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        blobs = BlobStore(tmp_path / "blobs")
        for expected_offset in (8, 16, 24):
            collect_dreal(http, db, blobs, REGION, REGION.dreal_listings, 0, backfill=True, max_pages=1)
            row = db.collection_coverage()[0]
            assert row["next_offset"] == expected_offset
            assert row["state"] == ("complete" if expected_offset == 24 else "partial")
        assert len([u for u in requests if "stockage-012667-a1.html" in u]) == 1
        collect_dreal(http, db, blobs, REGION, REGION.dreal_listings, 0)
        rows = {r["mode"]: r for r in db.collection_coverage()}
        assert rows["historical"]["state"] == "complete"
        assert rows["historical"]["pages"] == 3
        assert rows["recent"]["state"] == "partial"
        assert rows["recent"]["next_offset"] == 8
        finished = rows["historical"]["completed_at"]
        collect_dreal(http, db, blobs, REGION, REGION.dreal_listings, 0, backfill=True, max_pages=1)
        history = next(r for r in db.collection_coverage() if r["mode"] == "historical")
        assert history["completed_at"] == finished  # checking the first page cannot refresh the whole archive
        collect_dreal(http, db, blobs, REGION, REGION.dreal_listings, 0, backfill=True, max_pages=1, restart=True)
        history = next(r for r in db.collection_coverage() if r["mode"] == "historical")
        assert history["state"] == "partial" and history["next_offset"] == 8
        assert history["completed_at"] is None


@pytest.mark.parametrize("failure", ["blocked", "empty", "missing-number"])
def test_incomplete_page_never_advances_checkpoint(tmp_path: Path, failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/2026-r1.html":
            if failure == "empty":
                return httpx.Response(200, text="<p>Website changed</p>")
            if failure == "missing-number":
                return httpx.Response(
                    200, text='<a href="stockage-a1.html">Projet de stockage d’énergie par batteries</a>'
                )
            return httpx.Response(200, text=listing(0))
        if failure == "blocked":
            return httpx.Response(403)
        return httpx.Response(200, text='<a href="document.pdf">Formulaire</a>')

    with (
        Database(tmp_path / "state.sqlite") as db,
        PoliteHttp("test", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        if failure == "blocked":
            with pytest.raises(SiteBlockedError):
                collect_dreal(http, db, BlobStore(tmp_path / "blobs"), REGION, REGION.dreal_listings, 16, backfill=True)
        else:
            report = collect_dreal(
                http, db, BlobStore(tmp_path / "blobs"), REGION, REGION.dreal_listings, 16, backfill=True
            )
            assert report.errors
        row = db.collection_coverage()[0]
        assert row["next_offset"] == 0
        assert row["state"] == ("blocked" if failure == "blocked" else "partial")
        assert row["completed_at"] is None


def test_inventory_exposes_missing_years_and_staleness(tmp_path: Path) -> None:
    with Database(tmp_path / "state.sqlite") as db:
        now = datetime.now(UTC)
        with db.transaction():
            db.save_collection_coverage(
                {
                    "source": "dreal",
                    "region": REGION.slug,
                    "period": "2026",
                    "scope": "2026-r1.html",
                    "mode": "historical",
                    "state": "complete",
                    "updated_at": (now - timedelta(days=8)).isoformat(),
                }
            )
        report = inventory(db, REGION, [2024, 2026, 2027], now=now)
        assert any(
            r["source"] == "mrae" and r["period"] == "2024" and r["state"] == "unconfigured" for r in report["scopes"]
        )
        assert any(
            r["source"] == "dreal" and r["period"] == "2027" and r["state"] == "unconfigured" for r in report["scopes"]
        )
        assert any(r["status"] == "stale" and r["state"] == "complete" for r in report["scopes"])


def test_empty_mrae_is_not_complete(tmp_path: Path) -> None:
    region = replace(REGION, mrae_pages={2026: "https://example.test/annual"})
    with (
        Database(tmp_path / "state.sqlite") as db,
        PoliteHttp(
            "test", default_interval_s=0, transport=httpx.MockTransport(lambda _: httpx.Response(200, text=""))
        ) as http,
    ):
        assert collect_mrae(http, db, BlobStore(tmp_path / "blobs"), region, 2026).errors
        assert db.collection_coverage()[0]["state"] == "partial"
