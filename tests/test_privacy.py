"""GDPR minimisation: person names never reach stored texts or model inputs."""

from pathlib import Path

import pytest

from radar.classification.base import ClassificationInput
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, CheckStatus, SourceKind
from radar.domain.models import Candidate, Classification
from radar.domain.privacy import MASK, MASKED_PERSON, PersonScrubber, is_person_field, person_names
from radar.evaluation.privacy import audit_privacy
from radar.extraction.llm import Extracted, ExtractionInput
from radar.pipeline.filings import Services, build_filing
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import CompanyInfo, StaticLookups
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
    gold = GoldClassifier({"000001": GoldLabel("000001", Category.BESS)}, known_keys=db.filing_keys_with_documents())
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


@pytest.mark.parametrize(
    ("first", "last", "denomination"),
    [
        ("Ludovic", "Dubrillon", "Ludovic Dubrillon"),
        ("Ludovic", "Dubrillon", "LUDOVIC DUBRILLON"),
        ("Ludovic", "Dubrillon", "ludovic dubrillon"),
        ("Anne-Marie", "Talveroz", "Anne-Marie Talveroz"),
        ("Jean", "D'Anvorel", "Jean D'Anvorel"),
    ],
)
def test_natural_person_applicant_is_scrubbed_before_classification_and_storage(
    db: Database, blobs: BlobStore, first: str, last: str, denomination: str
) -> None:
    class RecordingClassifier:
        name = "synthetic-privacy-probe"

        def __init__(self) -> None:
            self.inputs: list[ClassificationInput] = []

        def classify(self, item: ClassificationInput) -> Classification:
            self.inputs.append(item)
            return Classification(category=Category.BESS, method=self.name)

    # Invented names: the denomination is a person, not a company exemption.
    fields = {
        "NomPersonneMorale1": last,
        "PrenomPersonneMorale1": first,
        "denomination": denomination,
        "raisonsociale": f"{first} {last}",
        "numeroSiret": "00000000000000",  # synthetic Luhn-valid value, never a real company lookup
        "NatureDuProjetY": f"Projet de stockage par batteries suivi par {first.lower()} {last.lower()}.",
    }
    with db.transaction():
        for filename, text, form in (
            ("privacy-cerfa.pdf", FILLER + fields["NatureDuProjetY"], fields),
            (
                "privacy-decision.pdf",
                FILLER
                + f" Article 1 er : Le stockage par batteries de {first} {last} n'est pas soumis à étude d'impact.",
                None,
            ),
        ):
            ingest_pdf(
                db,
                blobs,
                data=make_pdf([text], form),
                source=SourceKind.DREAL_CASE_BY_CASE,
                region="hauts-de-france",
                number="privacy-synthetic",
                url=f"https://example.org/{filename}",
                title="Projet de stockage par batteries",
                filename=filename,
            )
    classifier = RecordingClassifier()
    lookups = StaticLookups(companies={"000000000": CompanyInfo("000000000", f"{first} {last}", None, None, "1000")})
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, lookups=lookups))
    filing = db.filing("dreal_case_by_case:hauts-de-france:privacy-synthetic")
    assert filing is not None and filing.developer == MASKED_PERSON
    assert classifier.inputs
    assert next(check for check in filing.checks if check.name == "gdpr").status is CheckStatus.ALERT
    for token in (first.lower(), last.lower()):
        assert all(token not in (item.title + item.text).lower() for item in classifier.inputs)
        assert token not in filing.model_dump_json().lower()
    assert audit_privacy(db, blobs) == []
    # A stale or imported candidate must be caught even when the final value is masked.
    with db.transaction():
        db.upsert_filing(
            filing.model_copy(
                update={"candidates": {"developer": [Candidate(source="synthetic", value=f"{first} {last}")]}}
            )
        )
    assert any("candidate:developer:0" in finding for finding in audit_privacy(db, blobs))


def test_person_applicant_without_form_name_fields_is_not_a_company_exemption() -> None:
    scrubber = PersonScrubber.for_filing([], companies=["Ludovic Dubrillon"], people=["Ludovic Dubrillon"])
    assert scrubber.scrub("Suivi par ludovic dubrillon") == "Suivi par " + MASK


def test_company_exemptions_cover_complete_names_only_with_accent_variants() -> None:
    scrubber = PersonScrubber.for_filing([{"nom": "Dubrillon"}], companies=["DUBRILLON ENERGIE"])
    text = "Contact Dubrillon, pour DUBRILLON ÉNERGIE ; dubrillon energie."
    assert scrubber.scrub(text) == "Contact [nom masqué], pour DUBRILLON ÉNERGIE ; dubrillon energie."
    assert scrubber.leaks(text) == ["Dubrillon"]
    assert scrubber.leaks(scrubber.scrub(text) or "") == []


def test_losing_person_candidate_and_disagreement_are_masked_without_name_fields(
    db: Database, blobs: BlobStore, lookups: StaticLookups
) -> None:
    with db.transaction():
        for filename, text, fields in (
            (
                "cerfa.pdf",
                FILLER + "Projet de stockage par batteries de Copper Birch.",
                {"denomination": "Synthetic Storage SAS", "raisonsociale": "Copper Birch"},
            ),
            (
                "decision.pdf",
                FILLER + "Vu le formulaire d'examen au cas par cas n° 999999, depose complet le 05 janvier 2026, "
                "par Ludovic Dubrillon relatif au projet de stockage par batteries, sur la commune de Synthville ;",
                None,
            ),
        ):
            ingest_pdf(
                db,
                blobs,
                data=make_pdf([text], fields),
                source=SourceKind.DREAL_CASE_BY_CASE,
                region="synthetic",
                number="losing-person",
                url=f"https://example.org/{filename}",
                title="Stockage par batteries",
                filename=filename,
            )
    filing = build_filing(
        "dreal_case_by_case:synthetic:losing-person",
        db.documents("dreal_case_by_case:synthetic:losing-person"),
        Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=lookups),
    )
    assert filing.developer == "Synthetic Storage SAS" and filing.legal_name == "Copper Birch"
    assert any(item.code == "disagreement" and item.field == "developer" for item in filing.flags)
    assert "dubrillon" not in filing.model_dump_json().lower()


def test_llm_candidate_is_masked_even_when_structured_location_discards_its_value(
    db: Database, blobs: BlobStore, lookups: StaticLookups
) -> None:
    class SyntheticExtractor:
        name = "llm-extract:synthetic-only"

        def extract(self, item: ExtractionInput) -> Extracted:
            return {"developer": ("Ludovic Dubrillon", "Porteur : Ludovic Dubrillon")}

    key = "dreal_case_by_case:synthetic:late-person"
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=b"<html><body><p>Projet de stockage par batteries.</p><p>Porteur :</p><p>Ludovic Dubrillon</p></body></html>",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="synthetic",
            number="late-person",
            url="https://example.org/cerfa.html",
            title="Stockage par batteries",
            filename="cerfa.html",
        )
    # A deterministic test double activates the fallback; it is never used by the real pilot.
    classifier = GoldClassifier({key: GoldLabel("late-person", Category.BESS)}, known_keys=[key])
    filing = build_filing(
        key,
        db.documents(key),
        Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, extractor=SyntheticExtractor(), lookups=lookups),
    )
    assert filing.classification.category == Category.BESS
    assert filing.developer is None and filing.candidates["developer"]
    assert "dubrillon" not in filing.model_dump_json().lower()
