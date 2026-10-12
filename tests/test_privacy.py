"""GDPR minimisation: person names never reach stored texts or model inputs."""

from pathlib import Path

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, SourceKind
from radar.domain.privacy import MASK, PersonScrubber, is_person_field, person_names
from radar.evaluation.privacy import audit_privacy
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf

PERSON_FIELDS = {"NomPersonneMorale1": "DUPONT", "PrenomPersonneMorale1": "Jean-Marc", "9. Nom": "Martin"}


def test_person_fields() -> None:
    assert all(is_person_field(k) for k in [*PERSON_FIELDS, "nomPorteurProjet", "2.2. Prenom"])
    assert not any(is_person_field(k) for k in ["denomination", "9. Qualite signataire", "nomenclatureIcpe"])
    assert person_names({**PERSON_FIELDS, "denomination": "APAL MW"}) == {"DUPONT", "Jean-Marc", "Martin"}


def test_scrubber_masks_people_not_companies() -> None:
    scrub = PersonScrubber.for_filing(
        [PERSON_FIELDS], companies=["MARTIN ENERGIE"]
    )  # « Martin » is also a company word
    out = scrub.scrub(
        "Jean-Marc DUPONT (jm@apal.fr, 06 12 34 56 78) pour MARTIN ENERGIE ; reçu par Madame Claire LEROY."
    )
    assert out is not None
    assert "DUPONT" not in out and "Jean-Marc" not in out and "LEROY" not in out and "06 12" not in out
    assert "MARTIN ENERGIE" in out and "[courriel]" in out and MASK in out
    assert scrub.leaks(out) == []


def test_stored_filing_has_no_person_names(
    db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path
) -> None:
    fields = {
        **PERSON_FIELDS,
        "denomination": "APAL MW",
        "NatureDuProjetY": "Le projet consiste a construire un stockage par batteries de 47 MW suivi par Jean-Marc DUPONT.",
    }
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER + " Contact : Monsieur Jean-Marc DUPONT, 03 20 30 59 59."], fields),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="000001",
            url="https://example.org/f.pdf",
            title="Formulaire de demande d’examen au cas par cas, stockage d’énergie par batteries",
            filename="000001-cerfa.pdf",
        )
    gold = GoldClassifier({"000001": GoldLabel("000001", Category.BESS)})
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups))
    filing = db.filing("dreal_case_by_case:hauts-de-france:000001")
    assert filing is not None and "DUPONT" not in filing.model_dump_json()
    assert filing.developer == "APAL MW" and filing.power.mw_max == 47.0
    assert audit_privacy(db, blobs) == []


def test_phone_mask_spares_dates_and_case_numbers() -> None:
    scrub = PersonScrubber()
    for kept in ["05 11 2024 2024_8373", "le 28 07 2025 2025_9097", "07 10 2025 2025-8770"]:
        assert scrub.scrub(kept) == kept  # flattened cerfa header: filing date + case number
    for phone in ["Tél. : 03 20 30 59 59", "06.12.34.56.78", "0612345678", "+33 6 12 34 56 78"]:
        assert "[téléphone]" in (scrub.scrub(phone) or "")
