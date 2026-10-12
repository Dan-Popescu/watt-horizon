"""Shared fixtures. Tests never touch the network: HTTP goes through respx mocks, lookups through StaticLookups."""

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest

from radar.config import Settings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import CommuneInfo, CompanyInfo, StaticLookups

FILLER = "Texte administratif de remplissage pour simuler une vraie page. " * 6


def make_pdf(pages: list[str], fields: dict[str, str] | None = None) -> bytes:
    """Build a PDF: one entry per page ('' = blank page, i.e. a scan); optional form fields on page 1."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=9)
    for i, (name, value) in enumerate((fields or {}).items()):
        widget = pymupdf.Widget()
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        widget.field_name, widget.field_value = name, value
        widget.rect = pymupdf.Rect(40, 700 - 12 * i, 560, 710 - 12 * i)
        doc[0].add_widget(widget)
    data: bytes = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, offline=True, llm_model=None, use_tesseract=False, user_agent="radar-tests")


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    with Database(settings.db_path) as database:
        yield database


@pytest.fixture
def blobs(settings: Settings) -> BlobStore:
    return BlobStore(settings.blobs_dir)


AUBENTON = CommuneInfo("02031", "Aubenton", "02", lat=49.83, lon=4.20)
BONNEUIL = CommuneInfo("60088", "Bonneuil-les-Eaux", "60", lat=49.68, lon=2.24)
VERS_SUR_SELLE = CommuneInfo("80791", "Vers-sur-Selle", "80", lat=49.85, lon=2.24)
MERU = CommuneInfo("60395", "Méru", "60", lat=49.236, lon=2.134)


@pytest.fixture
def lookups() -> StaticLookups:
    return StaticLookups(
        companies={"824972772": CompanyInfo("824972772", "APAL MW", "35.11Z", "2016-12-21", "5710")},
        communes=[AUBENTON, BONNEUIL, VERS_SUR_SELLE, MERU],
        at={(49.848, 4.203): AUBENTON, (49.85, 2.248): VERS_SUR_SELLE},
    )


@pytest.fixture
def null_ocr() -> NullOcr:
    return NullOcr()
