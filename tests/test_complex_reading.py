"""Constructed table/OCR layouts test mechanisms; they are not human-verified references."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, SourceKind
from radar.extraction.observations import observe_quantities
from radar.extraction.supporting import extract_supporting
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.reading import ocr
from radar.reading.ocr import CachedOcr, NullOcr, TesseractOcr
from radar.reading.pdf import PdfContent, PdfReader, read_pdf
from radar.reading.secondary import SelectiveOcr
from radar.reading.tables import PdfTable
from radar.reading.workers import WorkResult
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf


def table_pdf(pages: int = 1) -> bytes:
    with pymupdf.open() as document:
        for _ in range(pages):
            page = document.new_page()
            xs, ys = (40, 250, 380, 550), (50, 85, 120)
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            for y, values in ((73, ("Projet", "Puissance", "Capacite")), (108, ("Stockage projet", "30 MW", "60 MWh"))):
                for x, text in zip(xs, values, strict=False):
                    page.insert_text((x + 8, y), text, fontsize=10)
        return bytes(document.tobytes())


def test_structured_table_cells_have_page_row_provenance_and_cache_policy(tmp_path: Path) -> None:
    data = table_pdf()
    cache = KeyValueCache(tmp_path / "cache.sqlite")
    plain = PdfReader(cache).read("doc", data)
    reader = PdfReader(cache, read_tables=True)
    rich = reader.read("doc", data)
    assert not plain.tables and rich.tables
    table = rich.tables[0]
    assert table.page == 1 and table.rows[1] == ("Stockage projet", "30 MW", "60 MWh")
    assert rich.coverage_summary()["table_rows"] == 2
    assert reader.read("doc", data) == rich
    result = extract_supporting([("doc", rich)])
    assert result.mw_max == 30 and result.mwh == 60


def test_table_page_and_row_limits_are_visible() -> None:
    result = read_pdf(table_pdf(2), read_tables=True, max_table_pages=1)
    assert result.table_pages == (1,) and 2 in result.table_errors
    truncated = read_pdf(table_pdf(), read_tables=True, max_table_rows=1)
    assert 1 in truncated.table_errors and not truncated.tables


def test_phase_asset_basis_and_header_units_remain_distinct() -> None:
    rows = (
        ("Objet", "Puissance (MW)", "Capacite (MWh)"),
        ("Stockage projet phase 1", "30", "60"),
        ("Stockage projet phase 2", "45", "90"),
    )
    text = (
        "Le projet photovoltaïque prévoit 100 MWdc. "
        "Le projet de stockage scénario B prévoit 40 MW et 80 MWh. "
        "Le stockage existant représente 10 MW."
    )
    content = PdfContent(1, {}, (text,), (), tables=(PdfTable(1, 1, rows, (0, 0, 100, 100)),))
    observations = observe_quantities([("doc", content)])
    assert any(o.asset == "solar" and o.basis == "dc" for o in observations)
    assert any(o.scenario == "b" and o.state == "proposed" for o in observations)
    assert any(o.state == "existing" for o in observations)
    assert {o.phase for o in observations if o.source_kind == "table"} == {"1", "2"}
    assert all(not o.eligible for o in observations)
    assert all(not o.unit_in_cell and o.header_quote for o in observations if o.source_kind == "table")
    assert extract_supporting([("doc", content)]).mw_max is None


def test_phase_column_and_grouped_header_values_are_preserved() -> None:
    rows = (("Objet", "Phase", "Puissance (MW)", "Capacite (MWh)"), ("Stockage projet", "2", "1 000", "2000"))
    content = PdfContent(1, {}, ("",), (), tables=(PdfTable(1, 1, rows, (0, 0, 100, 100)),))
    observations = observe_quantities([("doc", content)])
    assert {o.phase for o in observations} == {"2"}
    assert {(o.dimension, o.high) for o in observations} == {("power", 1000), ("energy", 2000)}
    assert all(not o.eligible for o in observations)


@pytest.mark.parametrize(
    "headers,row",
    [
        (("Installation", "Phase", "Puissance"), ("Projet de stockage", "1", "30 MW")),
        (("Installation", "Phase de construction", "Puissance"), ("Projet de stockage", "1", "30 MW")),
        (("Installation", "Scenario", "Puissance"), ("Projet de stockage", "A", "30 MW")),
        (("Installation", "Etat", "Puissance"), ("Projet de stockage", "existant", "30 MW")),
        (("Installation", "Actif", "Puissance"), ("Projet de stockage", "photovoltaïque", "30 MW")),
        (("Installation existante", "Puissance"), ("Projet de stockage", "30 MW")),
        (("Exemple d'installation", "Puissance"), ("Projet de stockage", "30 MW")),
        (("Installation", "Puissance (MWh)"), ("Projet de stockage", "30 MW")),
        (("Installation", "Puissance (MWdc)"), ("Projet de stockage", "30 MW")),
        (("Installation", "Puissance (kW)"), ("Projet de stockage", "30 MW")),
    ],
)
@pytest.mark.parametrize("flat_text", ["", "Projet de stockage 1 30 MW"])
def test_table_context_never_bypasses_scalar_promotion_guards(headers, row, flat_text: str) -> None:
    content = PdfContent(1, {}, (flat_text,), (), tables=(PdfTable(1, 1, (headers, row), (0, 0, 100, 100)),))
    observations = observe_quantities([("doc", content)])
    assert observations and all(not observation.eligible for observation in observations)
    result = extract_supporting([("doc", content)])
    assert result.mw_max is None and result.mwh is None


def test_clear_proposed_storage_table_row_still_supplies_literal_quantities() -> None:
    rows = (("Installation", "Puissance", "Capacite"), ("Projet de stockage", "30 MW", "60 MWh"))
    content = PdfContent(1, {}, ("",), (), tables=(PdfTable(1, 1, rows, (0, 0, 100, 100)),))
    result = extract_supporting([("doc", content)])
    assert result.mw_max == 30 and result.mwh == 60
    assert result.evidence["power"].locator == "page:1:table:1:row:2"


def test_quantity_observation_context_is_scrubbed_before_storage(db: Database, blobs: BlobStore) -> None:
    # Constructed person name deliberately appears in a phase label as well as the quote.
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=make_pdf(
                [FILLER + " Le projet de stockage phase DUPONT prévoit 30 MW."], {"NomPersonneMorale1": "DUPONT"}
            ),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="000001",
            url="https://example.org/annexe.pdf",
            title="Projet de stockage",
            filename="annexe.pdf",
        )
    classifier = GoldClassifier(
        {"000001": GoldLabel("000001", Category.BESS)}, known_keys=db.filing_keys_with_documents()
    )
    report = process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, lookups=StaticLookups()))
    assert not report.failed
    filing = db.filing("dreal_case_by_case:hauts-de-france:000001")
    assert filing is not None and filing.quantity_observations
    assert "dupont" not in filing.model_dump_json().lower()


class FixedOcr:
    def __init__(self, label: str, texts: dict[int, str]) -> None:
        self.label, self.texts, self.calls = label, texts, 0

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        self.calls += 1
        selected = {page: text for page, text in self.texts.items() if page in content.unread_scanned_pages}
        return replace(
            content,
            ocr_texts=content.ocr_texts | selected,
            ocr_engines=content.ocr_engines | dict.fromkeys(selected, self.label),
        )


def test_secondary_retry_improves_only_compatible_low_quality_pages() -> None:
    content = PdfContent(3, {}, ("", "", ""), (1, 2, 3))
    primary = FixedOcr("primary", {1: "30 MW", 2: "30 MW", 3: ""})
    secondary = FixedOcr(
        "secondary",
        {
            1: "Le projet de stockage par batteries prévoit une puissance de 30 MW.",
            2: "Le projet de stockage par batteries prévoit une puissance de 300 MW.",
            3: "Le projet de stockage par batteries est situé dans la commune.",
        },
    )
    result = SelectiveOcr(primary, secondary, max_pages=2).complete("doc", b"pdf", content)
    assert "batteries" in result.ocr_texts[1]
    assert result.ocr_texts[2] == "30 MW" and "disagrees" in result.ocr_errors[2]
    assert "budget" in result.ocr_errors[3] and result.ocr_engines[1] == "secondary"


def test_secondary_never_overwrites_transcripts_and_cache_reuses_retry(tmp_path: Path) -> None:
    content = PdfContent(1, {}, ("",), (1,))
    curated = SelectiveOcr(FixedOcr("transcript", {1: "30 MW"}), FixedOcr("secondary", {1: "300 MW"}))
    assert curated.complete("doc", b"pdf", content).ocr_texts[1] == "30 MW"
    inner = FixedOcr("secondary", {1: "Le projet de stockage prévoit une puissance de 30 MW."})
    cached = CachedOcr(inner, KeyValueCache(tmp_path / "ocr.sqlite"))
    engine = SelectiveOcr(FixedOcr("primary", {}), cached)
    first = engine.complete("doc", b"pdf", content)
    second = engine.complete("doc", b"pdf", content)
    assert first.ocr_texts == second.ocr_texts and inner.calls == 1
    assert second.coverage_summary()["ocr_cache_pages"] == 1


def test_empty_machine_ocr_does_not_mark_a_page_read(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "run_bounded", lambda *args, **kwargs: iter([("1", WorkResult(value="", started=True))]))
    content = PdfContent(1, {}, ("",), (1,))
    result = TesseractOcr().complete("doc", b"pdf", content)
    assert result.unread_scanned_pages == (1,) and result.ocr_errors[1]
    assert result.coverage_summary()["ocr_native_pages"] == 1


def test_abandoned_ocr_pages_are_requested_but_not_counted_as_started(monkeypatch) -> None:
    monkeypatch.setattr(
        ocr,
        "run_bounded",
        lambda *args, **kwargs: iter(
            [
                ("1", WorkResult(error="TimeoutError: worker group deadline exceeded")),
            ]
        ),
    )
    content = PdfContent(1, {}, ("",), (1,))
    result = TesseractOcr().complete("doc", b"pdf", content)
    assert result.coverage_summary()["ocr_requested_pages"] == 1
    assert result.coverage_summary()["ocr_native_pages"] == 0
    assert result.unread_scanned_pages == (1,)


def test_native_secondary_ocr_reads_a_constructed_scan() -> None:
    if not TesseractOcr.available():
        pytest.skip("Local Tesseract is optional")
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=5, check=False)
    available = languages.stdout.splitlines()
    language = "eng" if "eng" in available else "fra" if "fra" in available else None
    if language is None:
        pytest.skip("Constructed scan smoke requires an English or French language pack")
    with pymupdf.open() as source:
        page = source.new_page(width=320, height=140)
        page.insert_text((20, 40), "Project battery storage", fontsize=18)
        page.insert_text((20, 85), "Power 30 MW", fontsize=20)
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
        with pymupdf.open() as scanned:
            scanned.new_page(width=320, height=140).insert_image(page.rect, pixmap=pixels)
            data = bytes(scanned.tobytes())
    content = read_pdf(data)
    engine = SelectiveOcr(FixedOcr("primary", {}), TesseractOcr(language=language, dpi=400, document_timeout_s=15))
    result = engine.complete("scan", data, content)
    assert "30 MW" in result.text and not result.unread_scanned_pages
    assert result.coverage_summary()["ocr_native_pages"] == 1
