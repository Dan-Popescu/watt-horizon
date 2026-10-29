"""Private blind review protocol on synthetic documents, without live collection or models."""

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from radar.config import Settings
from radar.domain.privacy import PersonScrubber
from radar.evaluation.campaign import load_campaign, prepare
from radar.reading.pdf import PdfContent
from radar.reading.tables import PdfTable
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.web.campaign_app import MAX_BODY_BYTES, STATIC, _table_cells, build_app
from radar.web.campaign_session import ReviewSession
from tests.test_annotation_campaign import QUOTE, seed

HEADERS = {"X-Campaign-Review": "1"}


def review_campaign(db: Database, blobs: BlobStore, path: Path, *, double_rate: float = 0.5) -> Path:
    seed(db, blobs)
    seed(db, blobs, number="000002")
    prepare(db, path, double_rate=double_rate)
    return path


def completed_annotation(payload: dict[str, Any]) -> dict[str, Any]:
    annotation = payload["annotation"]
    annotation["reviewed_documents"] = list(annotation["document_relevance"])
    for observation in [*annotation["fields"].values(), *annotation["document_relevance"].values()]:
        observation["status"] = "unknown"
    document_id = annotation["reviewed_documents"][0]
    annotation["fields"]["power_mw"] = {
        "status": "present",
        "value": 0.05,
        "evidence": {"document_id": document_id, "page": 1, "locator": "text", "quote": QUOTE},
    }
    return annotation


def test_blind_queue_and_partial_revisions_do_not_expose_machine_values(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign")
    app = build_app(settings, directory, "Alice", "a")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        queue = client.get("/api/queue").json()
        key = queue["records"][0]["filing_key"]
        original = client.get("/api/record", params={"filing_key": key}).json()
        assert original["annotation"]["fields"]["power_mw"]["value"] is None
        assert original["annotation"]["reviewer"] == "" and "Alice" not in json.dumps(original)
        assert "machine" not in original and "partition" not in original
        annotation = original["annotation"]
        annotation["verified"] = True  # A draft cannot impersonate a final human decision.
        annotation["fields"]["power_mw"] = {"status": "present", "value": 0.05, "evidence": None}
        saved = client.post("/api/save", headers=HEADERS, json={"revision": 0, "annotation": annotation})
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1 and not saved.json()["annotation"]["verified"]
        stale = client.post("/api/save", headers=HEADERS, json={"revision": 0, "annotation": annotation})
        assert stale.status_code == 409
        spoofed = {**annotation, "reviewer": "Bob"}
        assert client.post("/api/save", headers=HEADERS, json={"revision": 1, "annotation": spoofed}).status_code == 400
        exported = directory / queue["export"]
        rows = [json.loads(line) for line in exported.read_text().splitlines()]
        assert all(not row["verified"] for row in rows)
    with sqlite3.connect(directory / "reviews/journal.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


def test_finalization_checks_real_source_and_preserves_approved_revision(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign", double_rate=0)
    app = build_app(settings, directory, "Alice", "a")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        key = client.get("/api/queue").json()["records"][0]["filing_key"]
        loaded = client.get("/api/record", params={"filing_key": key}).json()
        unfinished = client.post(
            "/api/save", headers=HEADERS, json={"revision": 0, "annotation": loaded["annotation"], "finalize": True}
        )
        assert unfinished.status_code == 400
        annotation = completed_annotation(loaded)
        quote = annotation["fields"]["power_mw"]["evidence"]["quote"]
        annotation["fields"]["power_mw"]["evidence"]["quote"] = "Une phrase absente du PDF."
        invalid = client.post(
            "/api/save", headers=HEADERS, json={"revision": 0, "annotation": annotation, "finalize": True}
        )
        assert invalid.status_code == 400
        annotation["fields"]["power_mw"]["evidence"]["quote"] = quote
        response = client.post(
            "/api/save",
            headers=HEADERS,
            json={"revision": 0, "annotation": annotation, "finalize": True, "duration_s": 15.0},
        )
        assert response.status_code == 200 and response.json()["finalized"]
        assert response.json()["annotation"]["verified"]
        assert (
            client.post("/api/save", headers=HEADERS, json={"revision": 1, "annotation": annotation}).status_code == 409
        )
        record = client.get("/api/record", params={"filing_key": key}).json()
        assert record["annotation"]["fields"]["power_mw"]["value"] == 0.05
    snapshots = list((directory / "reviews").glob("a-*-*.jsonl"))
    assert snapshots and any(json.loads(path.read_text().splitlines()[0])["verified"] for path in snapshots)
    with sqlite3.connect(directory / "reviews/journal.sqlite") as conn:
        assert conn.execute("SELECT finalized,duration_s FROM revisions").fetchone() == (1, 15.0)


def test_second_reader_cannot_reuse_identity_or_access_first_reader_dossiers(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign")
    ReviewSession(directory, "Alice", "a")
    with pytest.raises(ValueError, match="distinctes"):
        ReviewSession(directory, "ALICE", "b")
    app = build_app(settings, directory, "Bob", "b")
    manifest = load_campaign(directory)
    excluded = next(record for record in manifest.records if not record.double_review)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        queue = client.get("/api/queue").json()
        assert len(queue["records"]) == 1
        assert client.get("/api/record", params={"filing_key": excluded.filing_key}).status_code == 404
        doc_id = excluded.documents[0].id
        assert (
            client.get(f"/api/documents/{doc_id}/pages/1/text", params={"filing_key": excluded.filing_key}).status_code
            == 404
        )
        assert client.get("/reviews/journal.sqlite").status_code == 404
    with pytest.raises(ValueError, match="attribué"):
        ReviewSession(directory, "Mallory", "a")


def test_documents_are_allowlisted_and_rendered_with_bounds(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign")
    with TestClient(build_app(settings, directory, "Alice", "a"), base_url="http://127.0.0.1") as client:
        key = client.get("/api/queue").json()["records"][0]["filing_key"]
        row = client.get("/api/record", params={"filing_key": key}).json()
        doc_id = row["documents"][0]["id"]
        rendered = client.get(f"/api/documents/{doc_id}/pages/1.png", params={"filing_key": key})
        assert rendered.status_code == 200 and rendered.content.startswith(b"\x89PNG")
        assert client.get(f"/api/documents/{doc_id}/pages/2.png", params={"filing_key": key}).status_code == 400
        assert client.get("/api/documents/" + "f" * 64 + "/pages/1/text", params={"filing_key": key}).status_code == 404
        text = client.get(f"/api/documents/{doc_id}/pages/1/text", params={"filing_key": key}).json()
        assert QUOTE in text["text"] and "machine" not in text
        blobs.path_for(doc_id).write_bytes(b"corrupt")
        assert client.get(f"/api/documents/{doc_id}/pages/1/text", params={"filing_key": key}).status_code == 400


def test_auth_host_and_cross_origin_form_guards(
    db: Database,
    blobs: BlobStore,
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign")
    with pytest.raises(ValueError, match="jeton"):
        build_app(settings, directory, "Alice", "a", host="0.0.0.0")
    app = build_app(settings, directory, "Alice", "a", token="test-token")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/queue").status_code == 401
        auth = {"Authorization": "Bearer test-token"}
        assert client.get("/api/queue", headers=auth).status_code == 200
        assert client.get("/api/queue", headers=auth | {"Host": "attacker.example"}).status_code == 400
        assert client.post("/api/save", headers=auth, json={}).status_code == 403
        too_large = client.post(
            "/api/save",
            headers=auth | HEADERS | {"Content-Type": "application/json"},
            content=b" " * (MAX_BODY_BYTES + 1),
        )
        assert too_large.status_code == 413
        assert client.get("/api/queue", headers=auth).headers["Cache-Control"] == "no-store"


def test_private_review_javascript_has_no_syntax_error(tmp_path: Path) -> None:
    html = STATIC.read_text()
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert script is not None and "<select" not in html
    path = tmp_path / "campaign.js"
    path.write_text(script[1])
    subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=True)


def test_table_source_cells_keep_coordinates_and_explicit_bounds() -> None:
    pdf = PdfContent(
        1,
        {},
        ("",),
        (),
        tables=(PdfTable(1, 1, (("Puissance (MW)", "Capacité (MWh)"), ("0,05", "0,1")), (0, 0, 100, 100)),),
    )
    cells, truncated = _table_cells(pdf, 1, PersonScrubber())
    assert not truncated
    assert cells[2] == {"locator": "table:1:row:2:column:1", "text": "0,05"}
    assert cells[0]["text"] == "Puissance (MW)"
    large = PdfContent(
        1, {}, ("",), (), tables=(PdfTable(1, 1, tuple((str(i),) for i in range(1000)), (0, 0, 100, 100)),)
    )
    cells, truncated = _table_cells(large, 1, PersonScrubber())
    assert len(cells) == 200 and truncated


def test_durable_journal_repairs_export_after_projection_failure(
    db: Database,
    blobs: BlobStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = review_campaign(db, blobs, tmp_path / "campaign")
    session = ReviewSession(directory, "Alice", "a")
    record = next(iter(session.records.values()))
    project = session._project_export

    def unavailable() -> None:
        raise OSError("Simulated export storage failure")

    monkeypatch.setattr(session, "_project_export", unavailable)

    def must_not_read(_: str, __: bytes) -> PdfContent:
        raise AssertionError("Saving a draft should not run document reading")

    with pytest.raises(OSError, match="Simulated"):
        session.save(record.filing_key, session.blank(record), 0, False, 2.0, blobs, must_not_read)
    assert session.load(record.filing_key)["revision"] == 1
    monkeypatch.setattr(session, "_project_export", project)
    session.queue()
    exported = [json.loads(line) for line in session.export_path.read_text().splitlines()]
    assert next(row for row in exported if row["filing_key"] == record.filing_key)["reviewer"] == "Alice"
    with sqlite3.connect(session.journal) as conn:
        assert conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
