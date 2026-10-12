"""Shared fixtures. Tests never touch the network: HTTP goes through respx mocks, lookups through StaticLookups."""

from collections.abc import Iterator
from pathlib import Path

import pymupdf
import pytest

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.config import Settings
from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.models import Signal
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import NullOcr, TranscriptOcr
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


# End-to-end dataset: a BESS form + decision, a scanned data-center decision, an off-topic filing, a signal.
DECISION_TEXT = (
    FILLER + "Vu le formulaire d’examen au cas par cas n° 010504/KK P, déposé complet le 05 janvier 2026, par la "
    "société APAL MW relatif au projet de centrale de stockage d’énergie par batteries des HOQUINS, sur la commune "
    "d’Aubenton, dans le département de l’Aisne ; Article 1 er : Le projet n’est pas soumis à étude d’impact."
)
FORM_FIELDS = {
    "denomination": "APAL MW",
    "numeroSiret": "82497277200027",
    "Dossiercompletdate": "05-01-2026",
    "NatureDuProjetY": "Le projet consiste a construire et exploiter un projet de stockage par batteries de 100 MW / 200 MWh "
    "sur la commune d'Aubenton. Il est situe au plus proche du poste RTE « Les Hoquins ». 36 conteneurs batteries.",
    "Valeurs": "(1.2, Ha) (11500, m²) (100, MW) (200, MWh)",
    "LocalityNameShared": "Aubenton",
    "lat1Forprojectaddress": "49°50'52\"1",
    "long1ForprojectAddress": "4°12'11\"9",
}


def _ingest(db: Database, blobs: BlobStore, data: bytes, number: str, filename: str, title: str) -> str:
    with db.transaction():
        return ingest_pdf(
            db,
            blobs,
            data=data,
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number=number,
            url=f"https://example.org/{filename}",
            title=title,
            filename=filename,
        ).id


@pytest.fixture
def loaded_db(db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path) -> Database:
    _ingest(
        db,
        blobs,
        make_pdf([FILLER, FILLER], FORM_FIELDS),
        "010504",
        "0010504-cerfa.pdf",
        "Formulaire de demande d’examen au cas par cas, relatif à un projet de centrale de stockage d’énergie par batteries",
    )
    _ingest(
        db,
        blobs,
        make_pdf([DECISION_TEXT]),
        "010504",
        "010504kkp_decision.pdf",
        "Décision de non soumission à la réalisation d’une étude d’impact du projet de centrale de stockage",
    )
    # A scanned decision with a transcription, and an off-topic filing dropped by the screen.
    scan_id = _ingest(
        db,
        blobs,
        make_pdf([""]),
        "2025-4009",
        "2025_4009.pdf",
        "Décision de non soumission à la réalisation d’une étude d’impact pour l’installation d’un datacenter, sur le site SIG de Béthune (62)",
    )
    (tmp_path / "tr").mkdir()
    (tmp_path / "tr" / f"{scan_id}.txt").write_text(
        "Vu le formulaire d'examen au cas par cas n°2025-4009, déposé complet le 17 novembre 2025 par la société SIG "
        "REVERSE INDUSTRY située à Béthune, concernant l'installation d'un datacenter au droit du site ;",
        encoding="utf-8",
    )
    _ingest(
        db,
        blobs,
        make_pdf([FILLER + " Projet de centrale photovoltaïque au sol."]),
        "2025-9999",
        "2025-9999-decision.pdf",
        "Décision relative à une centrale photovoltaïque",
    )
    with db.transaction():
        db.upsert_signal(
            Signal(
                key="company:999",
                kind=SignalKind.COMPANY,
                source=SourceKind.COMPANY_REGISTRY,
                title="APAL MW STORAGE 2",
                url="https://example.org/c",
                developer="APAL MW STORAGE 2",
            )
        )
    gold = {"010504": GoldLabel("010504", Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)}
    services = Services(
        blobs=blobs, ocr=TranscriptOcr(tmp_path / "tr"), classifier=GoldClassifier(gold), lookups=lookups
    )
    report = process_filings(db, services)
    assert report.processed == 3 and not report.failed
    assert link_and_build(db)["projects"] == 2
    return db
