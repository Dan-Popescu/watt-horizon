"""Constructed PDF cells verify table citations without inventing inline source units."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from radar.domain.enums import SourceKind
from radar.domain.models import Filing
from radar.domain.quantity_context import QuantityObservation
from radar.evaluation import campaign_review
from radar.evaluation.campaign import load_campaign, prepare
from radar.evaluation.campaign_review import _table_value, validate
from radar.evaluation.privacy import audit_privacy
from radar.pipeline.ingest import ingest_pdf
from radar.reading.pdf import PdfReader
from radar.reading.tables import PdfTable
from radar.store.blobs import BlobStore
from radar.store.db import Database


def source_table() -> bytes:
    with pymupdf.open() as document:
        for _ in range(3):
            document.new_page().insert_text((40, 70), "Other page without a table")
        page = document.new_page(width=540, height=200)
        xs, ys = (40, 230, 370, 520), (50, 90, 130, 170)
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for y, values in (
            (75, ("Objet", "Puissance (kW)", "Capacite (MWh)")),
            (115, ("Stockage phase 2", "500", "1")),
            (155, ("Stockage phase 1", "250", "0.5")),
        ):
            for x, value in zip(xs, values, strict=False):
                page.insert_text((x + 5, y), value, fontsize=10)
        return bytes(document.tobytes())


def test_header_unit_citation_verifies_only_requested_page_and_preserves_quote(
    db: Database, blobs: BlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db.transaction():
        document = ingest_pdf(
            db,
            blobs,
            data=source_table(),
            source=SourceKind.MRAE_OPINION,
            region="test",
            number="1",
            url="https://example.org/opinion.pdf",
            title="Projet de stockage",
            filename="opinion.pdf",
            fetched_at=datetime.now(UTC),
        )
        filing = Filing(
            key=document.filing_key,
            source=document.source,
            region="test",
            number="1",
            title="Projet de stockage",
            document_ids=(document.id,),
        )
        db.upsert_filing(filing)
        db.note_processing_outcome(filing.key, "complete")
    directory = tmp_path / "campaign"
    prepare(db, directory, double_rate=0)
    manifest = load_campaign(directory)
    row = json.loads((directory / "reviewer-a.jsonl").read_text())
    row.update(reviewer="Reader", verified=True, blind=True, reviewed_documents=[document.id])
    for field, value, quote, column in (("power_mw", 0.5, "500", 2), ("energy_mwh", 1.0, "1", 3)):
        row["fields"][field] = {
            "status": "present",
            "value": value,
            "evidence": {
                "document_id": document.id,
                "page": 4,
                "locator": f"table:1:row:2:column:{column}",
                "quote": quote,
            },
        }
    path = directory / "reader.jsonl"
    path.write_text(json.dumps(row))
    real = campaign_review.run_bounded
    requested = []

    def bounded(function, jobs, **kwargs):
        jobs = list(jobs)
        requested.extend(arguments[1] for _, arguments in jobs)
        return real(function, jobs, **kwargs)

    monkeypatch.setattr(campaign_review, "run_bounded", bounded)
    reader = PdfReader(read_tables=False)
    report, references = validate(manifest, [path], blobs, read=reader.read)
    assert references[filing.key] == {"power_mw": 0.5, "energy_mwh": 1.0}
    assert report["accepted_fields"] == 2 and requested == [4]
    assert row["fields"]["power_mw"]["evidence"]["quote"] == "500"  # no synthesized "500 kW" quote


@pytest.mark.parametrize(
    "header,cell,field,value,quote",
    [
        ("Puissance (kW)", "500", "power_mw", 500.0, "500"),
        ("Puissance (kW)", "500", "power_mw", 0.5, "500 kW"),
        ("Puissance (MW/MWh)", "30", "power_mw", 30.0, "30"),
        ("Capacite (MWh)", "30", "power_mw", 30.0, "30"),
        ("Puissance (MWdc)", "30", "power_mw", 30.0, "30"),
        ("Puissance (MWdc)", "30 MW", "power_mw", 30.0, "30 MW"),
        ("Puissance (kW)", "30 MW", "power_mw", 30.0, "30 MW"),
        ("Puissance", "30", "power_mw", 30.0, "30"),
        ("Capacite (MWh)", "10 à 20", "energy_mwh", 20.0, "10 à 20"),
    ],
)
def test_ambiguous_or_invented_table_proofs_are_refused(
    header: str, cell: str, field: str, value: float, quote: str
) -> None:
    table = PdfTable(1, 1, ((header,), (cell,)), (0, 0, 100, 100))
    with pytest.raises(ValueError):
        _table_value(field, value, quote, table, 2, 1)


def test_header_ranges_and_basis_are_normalized_without_phase_selection() -> None:
    table = PdfTable(1, 1, (("Puissance (kW)",), ("250 à 500",)), (0, 0, 100, 100))
    _table_value("power_mw", 0.5, "250 à 500", table, 2, 1)
    _table_value("power_mw_min", 0.25, "250 à 500", table, 2, 1)
    with pytest.raises(ValueError, match="outside"):
        _table_value("power_mw", 0.5, "250 à 500", table, 9, 1)
    with pytest.raises(ValueError, match="header"):
        _table_value("power_mw", 0.5, "Puissance (kW)", table, 1, 1)


def test_privacy_audit_includes_all_quantity_context_strings(db: Database, blobs: BlobStore) -> None:
    observation = QuantityObservation(
        document_id="constructed",
        page=1,
        locator="page:1",
        low=1,
        high=1,
        dimension="power",
        asset="unknown",
        state="unknown",
        source_kind="text",
        quote="Contact reader@example.org",
        header_quote="Téléphone 06 12 34 56 78",
        phase="Monsieur Jean DUPONT",
        scenario="Madame Anne MARTIN",
    )
    filing = Filing(
        key="manual:test:1",
        source=SourceKind.MANUAL,
        region="test",
        number="1",
        title="Constructed",
        document_ids=(),
        quantity_observations=(observation,),
    )
    with db.transaction():
        db.upsert_filing(filing)
    leaks = audit_privacy(db, blobs)
    assert {leak.rsplit(":", 1)[-1] for leak in leaks} == {"quote", "header", "phase", "scenario"}
