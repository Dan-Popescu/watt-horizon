"""Human review loop: corrections win over the machine, the machine output stays measurable, reviews become
verified references, and the web review mode exposes it all."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from radar.api.review import export_corrections, import_corrections, review_items, submit_review
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, Confidence, PowerStatus
from radar.domain.models import Correction
from radar.evaluation.fields import score_fields
from radar.pipeline.corrections import apply_corrections, reviewed_references, validate_value, with_machine
from radar.pipeline.filings import Services
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from radar.web.app import build_app

AUBENTON_KEY = "dreal_case_by_case:hauts-de-france:010504"
T0 = datetime(2026, 10, 12, 9, tzinfo=UTC)


@pytest.fixture
def services(blobs: BlobStore, lookups: StaticLookups) -> Services:
    gold = {"010504": GoldLabel("010504", Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)}
    return Services(blobs=blobs, ocr=NullOcr(), classifier=GoldClassifier(gold), lookups=lookups)


def correction(field: str, value: str | float | None, minutes: int = 0, cid: int = 1) -> Correction:
    return Correction(
        id=cid, filing_key=AUBENTON_KEY, field=field, value=value, note="lu p. 3", author="Dan",
        created_at=T0.replace(minute=minutes),
    )  # fmt: skip


def test_apply_corrections_keeps_machine_output(loaded_db: Database, lookups: StaticLookups) -> None:
    filing = loaded_db.filing(AUBENTON_KEY)
    assert filing is not None
    corrected = apply_corrections(
        with_machine(filing),
        [correction("power_mw", 120.0), correction("power_mw", 110.0, minutes=5, cid=2), correction("validated", None)],
        lookups,
    )
    assert corrected.power.mw_max == 110.0 and corrected.power.status is PowerStatus.KNOWN  # latest wins
    assert corrected.machine is not None and corrected.machine.values["power_mw"] == 100.0
    assert corrected.evidence["power"].document_id == "human" and corrected.confidence is Confidence.HIGH
    relocated = apply_corrections(with_machine(filing), [correction("commune", "Méru")], lookups)
    assert relocated.location.insee_code == "60395" and relocated.location.precision == "commune"


@contextmanager
def db_unclassified(db: Database) -> Iterator[str]:
    filing = db.filing(AUBENTON_KEY)
    assert filing is not None
    unclassified = filing.classification.model_copy(update={"category": Category.UNCLASSIFIED})
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"classification": unclassified}))
    yield AUBENTON_KEY
    with db.transaction():
        db.upsert_filing(filing)


def test_validate_value() -> None:
    assert (
        validate_value("power_mw", "47,5") == 47.5 and validate_value("siret", "824 972 772 00027") == "82497277200027"
    )
    assert validate_value("substation", "") is None  # "absent from the documents"
    for name, value in [
        ("power_mw", "-3"),
        ("category", "unicorn"),
        ("category", "unclassified"),
        ("filed_on", "hier"),
        ("color", "x"),
    ]:
        with pytest.raises(ValueError):
            validate_value(name, value)


def test_review_loop_end_to_end(loaded_db: Database, services: Services) -> None:
    submit_review(loaded_db, AUBENTON_KEY, author="Dan", changes={"power_mw": "120"}, note="M. Paul X", validate=True)
    process_filings(loaded_db, services, only={AUBENTON_KEY})
    link_and_build(loaded_db)

    filing = loaded_db.filing(AUBENTON_KEY)
    assert filing is not None and filing.power.mw_max == 120.0 and filing.reviewed_at is not None
    assert "Paul" not in filing.evidence["power"].quote  # notes are GDPR-scrubbed too
    labels, fields = reviewed_references(loaded_db.filings(), loaded_db.corrections())
    assert labels["010504"].verified and labels["010504"].category is Category.BESS
    assert fields["010504"].expected["power_mw"] == 120.0 and fields["010504"].verified
    score = score_fields(loaded_db.filings(), fields, verified=True)["power_mw"]
    assert score.wrong == 1  # the machine said 100: evaluation measures the machine, not the correction
    [item] = [i for i in review_items(loaded_db, {}) if i.key == AUBENTON_KEY]
    assert item.machine_values["power_mw"] == 100.0 and item.values["power_mw"] == 120.0

    with db_unclassified(loaded_db) as key, pytest.raises(ValueError, match="catégorie"):
        submit_review(loaded_db, key, author="Dan", changes={}, validate=True)
    with pytest.raises(ValueError, match="nothing to record"):
        submit_review(loaded_db, AUBENTON_KEY, author="Dan", changes={})
    with pytest.raises(ValueError, match="author"):
        submit_review(loaded_db, AUBENTON_KEY, author=" ", changes={}, validate=True)


def test_web_review_mode(loaded_db: Database, services: Services) -> None:
    def rebuild(db: Database, key: str) -> None:
        process_filings(db, services, only={key})
        link_and_build(db)

    read_only = TestClient(build_app(loaded_db.path, with_mcp=False), base_url="http://127.0.0.1:8765")
    assert read_only.get("/api/review").status_code == 404
    assert read_only.post(f"/api/review/{AUBENTON_KEY}/corrections", json={}).status_code == 404

    client = TestClient(
        build_app(loaded_db.path, with_mcp=False, review=(rebuild, dict)), base_url="http://127.0.0.1:8765"
    )
    items = client.get("/api/review").json()
    assert {i["key"] for i in items} >= {AUBENTON_KEY}
    body = {"author": "Dan", "changes": {"substation": "Hoquins 2"}, "validate": True}
    updated = client.post(f"/api/review/{AUBENTON_KEY}/corrections", json=body).json()
    assert updated["values"]["substation"] == "Hoquins 2" and updated["machine_values"]["substation"] == "Les Hoquins"
    bad = client.post(f"/api/review/{AUBENTON_KEY}/corrections", json={"author": "Dan", "changes": {"power_mw": "-1"}})
    assert bad.status_code == 400
    project = client.get("/api/projects", params={"category": "bess"}).json()[0]
    assert project["substation"] == "Hoquins 2" and project["confidence"] == "high"

    substation_fix = next(c["id"] for c in updated["corrections"] if c["field"] == "substation")
    assert client.delete(f"/api/review/corrections/{substation_fix}").status_code == 200
    assert client.get("/api/projects", params={"category": "bess"}).json()[0]["substation"] == "Les Hoquins"
    assert client.delete(f"/api/review/corrections/{substation_fix}").status_code == 404


def test_corrections_export_import_roundtrip(loaded_db: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    submit_review(loaded_db, AUBENTON_KEY, author="Dan", changes={"substation": "Hoquins 2"}, validate=True, now=T0)
    rows = export_corrections(loaded_db)
    assert len(rows) == 2 and all("id" not in r for r in rows)
    fresh = Database(tmp_path / "fresh.sqlite")
    assert import_corrections(fresh, rows) == 2 and import_corrections(fresh, rows) == 0  # idempotent
    assert {c.field for c in fresh.corrections()} == {"substation", "validated"}
