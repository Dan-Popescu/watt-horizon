"""End to end on generated PDFs: ingest -> process -> link -> projects -> queries -> MCP tools."""

from pathlib import Path

import pytest
from mcp import Client

from radar.api import queries
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, Confidence, PowerStatus, ReviewDecision, SignalKind, SourceKind
from radar.domain.models import Signal
from radar.mcp_server import build_server
from radar.pipeline.evaluate import evaluate
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import TranscriptOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf

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

    result = evaluate(loaded_db.filings(), {"010504": GoldLabel("010504", Category.BESS)})
    assert result.screening_recall == 1.0


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
