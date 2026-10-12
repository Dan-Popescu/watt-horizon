"""Evidence shown in the PDF: every value read from the text layer or a form field is located on its page."""

from pathlib import Path

import pymupdf
import pytest
from starlette.testclient import TestClient

from radar.quality import corpus, pageview
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.web.app import build_app

CORPUS = Path(__file__).parent / "corpus"


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("corpus")
    corpus.replay(CORPUS, work)
    return work


def test_every_document_evidence_is_located(replayed: Path) -> None:
    blobs = BlobStore(replayed / "blobs")
    missed = []
    with Database(replayed / "corpus.sqlite") as db:
        for f in db.filings():
            for name, ev in f.evidence.items():
                if ev.locator == "title" or ev.document_id == "human":
                    continue  # titles come from the listing page, not from the PDF
                view = pageview.render(blobs.get(ev.document_id), ev.quote, ev.locator)
                if not view.found:
                    missed.append(f"{f.number} {name} {ev.locator}")
                assert view.png.startswith(b"\x89PNG") and 1 <= view.page <= view.pages
    assert not missed


def test_locate_dehyphenated_and_masked_quotes(tmp_path: Path) -> None:
    doc = pymupdf.open()  # type: ignore[no-untyped-call]
    page = doc.new_page()
    page.insert_text((50, 100), "Le projet de stockage d'électricité par batte-")
    page.insert_text((50, 115), "ries est porté par Monsieur Durand pour la société ACME.")
    n, rects, fields = pageview.locate(
        doc, "stockage d'électricité par batteries est porté par Monsieur [nom masqué]", "text"
    )
    assert (n, fields) == (0, False) and rects


def test_endpoint_only_in_review_mode(replayed: Path) -> None:
    with Database(replayed / "corpus.sqlite") as db:
        f = next(f for f in db.filings() if "power" in f.evidence)
    off = TestClient(build_app(replayed / "corpus.sqlite", with_mcp=False), base_url="http://127.0.0.1")
    assert off.get(f"/api/review/{f.key}/evidence/power.png").status_code == 404
    app = build_app(
        replayed / "corpus.sqlite",
        with_mcp=False,
        review=(lambda db, key: None, dict),
        document=BlobStore(replayed / "blobs").get,
    )
    res = TestClient(app, base_url="http://127.0.0.1").get(f"/api/review/{f.key}/evidence/power.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"
    assert res.headers["X-Radar-Found"] == "1"
