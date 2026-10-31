"""GDPR minimisation: person names never reach stored texts or model inputs."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.classification.base import ClassificationInput
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, CheckStatus, DocumentRole, SourceKind
from radar.domain.models import Candidate, Classification, DocumentRecord
from radar.domain.privacy import MASK, MASKED_PERSON, PersonScrubber, is_person_field, person_names
from radar.evaluation.privacy import audit_privacy
from radar.extraction.llm import Extracted, ExtractionInput
from radar.pipeline.filings import Services, build_filing
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.reading.ocr import NullOcr
from radar.reading.pdf import PdfContent, PdfReader
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


@pytest.mark.parametrize("first", ["Ab", "Abe"])
def test_complete_known_person_with_short_token_is_not_a_company_exemption(first: str) -> None:
    # Invented names; the two-letter case must receive the same protection as
    # the three-letter control, without globally masking a common short word.
    applicant = f"{first} Exampleon"
    scrubber = PersonScrubber.for_filing([], [applicant], people=iter([applicant]))
    assert not scrubber.keep
    assert scrubber.scrub(f"Contact : {applicant}") == f"Contact : {MASK}"
    assert scrubber.scrub(f"Contact : {applicant.lower()}") == f"Contact : {MASK}"


def test_complete_person_span_spares_complete_company_spans_with_shared_short_token() -> None:
    scrubber = PersonScrubber.for_filing(
        [{"Nom": "Exampleon"}],
        ["AB EXAMPLEON POWER", "EXAMPLEON ENERGIE"],
        people=["Ab Exampleon"],
    )
    source = "Ab Exampleon ; AB EXAMPLEON POWER ; ab-exampleon power ; EXAMPLEON ÉNERGIE ; Ab Exampleon."
    expected = f"{MASK} ; AB EXAMPLEON POWER ; ab-exampleon power ; EXAMPLEON ÉNERGIE ; {MASK}."
    assert scrubber.scrub(source) == expected
    assert scrubber.scrub(expected) == expected
    assert scrubber.leaks(expected) == []


@pytest.mark.parametrize("separator", [" ; ", ". ", " — ", " "])
@pytest.mark.parametrize("first", ["Qe", "Qex"])
def test_person_match_cannot_cross_into_a_complete_company_span(first: str, separator: str) -> None:
    scrubber = PersonScrubber.for_filing([], ["ZAXNOR POWER"], people=[f"{first} Zaxnor"])
    source = f"Contact {first}{separator}ZAXNOR POWER"
    # The complete identity provides context for the short first name, while
    # the overlapping company is preserved. This is not a global short-token rule.
    assert scrubber.scrub(source) == f"Contact {MASK}{separator}ZAXNOR POWER"


def test_short_person_part_stays_visible_without_the_complete_identity_context() -> None:
    scrubber = PersonScrubber.for_filing([], ["ZAXNOR POWER"], people=["Qe Zaxnor"])
    source = "Contact Qe ; secteur distinct ; ZAXNOR POWER"
    assert scrubber.scrub(source) == source


def test_known_person_prefix_outside_a_company_is_masked_after_whitespace_flattening() -> None:
    scrubber = PersonScrubber.for_filing([], ["EXAMPLEONENERGIE"], people=["Ab Exampleon"])
    source = "Contact Ab Exampleon Énergie : capacité annoncée."
    assert scrubber.scrub(source) == f"Contact {MASK} Exampleon Énergie : capacité annoncée."
    assert scrubber.leaks(source) == ["Ab"]
    assert scrubber.leaks(scrubber.scrub(source) or "") == []


def test_overlapping_company_spans_preserve_only_the_parts_of_the_complete_company() -> None:
    scrubber = PersonScrubber.for_filing([], ["ALPHA LI", "QE POWER"], people=["Li Wu Qe"])
    source = "ALPHA LI Wu QE POWER"
    assert scrubber.scrub(source) == f"ALPHA LI {MASK} QE POWER"
    assert scrubber.leaks(source) == ["Wu"]


def test_known_person_suffix_outside_a_company_is_masked_without_masking_its_prefix() -> None:
    scrubber = PersonScrubber.for_filing([], ["ALPHA LI"], people=["Li Wu"])
    source = "ALPHA LI Wu"
    assert scrubber.scrub(source) == f"ALPHA LI {MASK}"
    assert scrubber.leaks(source) == ["Wu"]


def test_separate_short_companies_cannot_jointly_exempt_a_complete_known_person() -> None:
    scrubber = PersonScrubber.for_filing([], ["LI", "WU"], people=["Li Wu"])
    assert scrubber.scrub("LI WU") == MASK
    assert scrubber.leaks("LI WU") == ["LI WU"]


def test_one_whole_company_remains_protected_among_short_company_aliases() -> None:
    scrubber = PersonScrubber.for_filing([], ["LI", "WU", "LI WU POWER"], people=["Li Wu"])
    source = "LI WU POWER"
    assert scrubber.scrub(source) == source
    assert scrubber.leaks(source) == []


def test_complete_person_span_accepts_whitespace_accents_and_source_separators() -> None:
    scrubber = PersonScrubber.for_filing([], people=["Éq Exampleon"])
    assert scrubber.scrub("eq\nexampleon") == MASK
    assert scrubber.scrub("eq ; Exampleon") == MASK


def test_complete_people_do_not_turn_combined_short_tokens_into_a_personal_company() -> None:
    scrubber = PersonScrubber.for_filing([], ["QE QEX"], people=["Qe Zaxnor", "Qex Zy"])
    assert scrubber.keep == frozenset({"QE QEX"})
    assert scrubber.scrub("Société QE QEX ; Qe Zaxnor ; Qex Zy") == f"Société QE QEX ; {MASK} ; {MASK}"


@pytest.mark.parametrize("as_company", [False, True])
@pytest.mark.parametrize("variant", ["Li Wu", "LiWu", "liwu", "LIWU", "Li-Wu", "Li_Wu", "Li’Wu", "Li  Wu"])
def test_complete_short_identity_ocr_variants_are_masked(variant: str, as_company: bool) -> None:
    scrubber = PersonScrubber.for_filing([], [variant] if as_company else [], people=["Li Wu"])
    assert scrubber.scrub(f"Contact : {variant}") == f"Contact : {MASK}"
    assert not scrubber.keep


@pytest.mark.parametrize("variant", ["QeD'Zy", "QeD’Zy", "QeDZy", "Qe-D-Zy", "qe d zy"])
def test_complete_identity_accepts_ocr_apostrophe_and_spacing_variants(variant: str) -> None:
    scrubber = PersonScrubber.for_filing([], [variant], people=["Qe D'Zy"])
    assert scrubber.scrub(variant) == MASK
    assert not scrubber.keep


@pytest.mark.parametrize(
    ("company", "source"),
    [
        ("LI WU INDUSTRIE", "LiWu INDUSTRIE"),
        ("LIWU INDUSTRIE", "Li Wu INDUSTRIE"),
        ("LIWU INDUSTRIE", "Li_Wu INDUSTRIE"),
        ("LIWU INDUSTRIE", "Li-Wu INDUSTRIE"),
        ("QEDZY SOLAR", "Qe D'Zy SOLAR"),
    ],
)
def test_long_distinct_company_survives_identity_ocr_variants(company: str, source: str) -> None:
    scrubber = PersonScrubber.for_filing([], [company], people=["Li Wu", "Qe D'Zy"])
    assert scrubber.scrub(source) == source
    assert scrubber.leaks(source) == []


@pytest.mark.parametrize("company", ["EXAMPLEONENERGIE", "EXAMPLEON ENERGIE"])
@pytest.mark.parametrize(
    "separator",
    [". ", "; ", ": ", " — ", " – ", "\n", "\r\n", "\t", " - ", " -- ", " - - ", " _ ", " ' ", " ’ ", "\u00a0-\u00a0"],
)
def test_company_exemption_cannot_cross_sentence_or_field_boundaries(company: str, separator: str) -> None:
    # Invented identity: the corporate suffix is the following heading here.
    scrubber = PersonScrubber.for_filing([], [company], people=["Ab Exampleon"])
    source = f"Contact Ab Exampleon{separator}Énergie : capacité annoncée."
    assert scrubber.scrub(source) == f"Contact {MASK}{separator}Énergie : capacité annoncée."
    assert scrubber.leaks(source)
    assert scrubber.leaks(scrubber.scrub(source) or "") == []


@pytest.mark.parametrize("variant", ["ExampleonEnergie", "EXAMPLEON ENERGIE", "Exampleon-Energie", "Exampleon_Énergie"])
def test_complete_company_ocr_spelling_stays_protected_beside_a_known_person(variant: str) -> None:
    scrubber = PersonScrubber.for_filing([], ["EXAMPLEONENERGIE"], people=["Ab Exampleon"])
    source = f"Contact Ab Exampleon. Société : {variant}."
    assert scrubber.scrub(source) == f"Contact {MASK}. Société : {variant}."


@pytest.mark.parametrize(
    "company",
    [
        "EXAMPLEON S.A.S.",
        "EXAMPLEON & PARTNERS",
        "EXAMPLEON (FRANCE)",
        "EXAMPLEON - ENERGIE",
        "EXAMPLEON _ ENERGIE",
        "EXAMPLEON ' ENERGIE",
    ],
)
def test_explicit_punctuation_in_a_known_legal_name_remains_protected(company: str) -> None:
    scrubber = PersonScrubber.for_filing([], [company], people=["Ab Exampleon"])
    source = f"Contact Ab Exampleon. Société : {company}."
    assert scrubber.scrub(source) == f"Contact {MASK}. Société : {company}."


@pytest.mark.parametrize("separator", [". ", "; ", " - ", "\n", "\t"])
def test_adjacent_administrative_heading_cannot_reexempt_person_before_classification(
    db: Database, blobs: BlobStore, lookups: StaticLookups, separator: str
) -> None:
    class RecordingClassifier:
        def __init__(self) -> None:
            self.inputs: list[ClassificationInput] = []

        def classify(self, item: ClassificationInput) -> Classification:
            self.inputs.append(item)
            return Classification(category=Category.BESS, method="synthetic-company-boundary")

    # Both identity and denomination are invented; the suffix is a heading,
    # not part of the person's name or the company mentioned in the form.
    identity = "Ab Exampleon"
    decision = (
        "Vu le formulaire d'examen au cas par cas n° 999999, déposé complet le 05 janvier 2026, "
        f"par {identity} relatif au projet de stockage par batteries, sur la commune de Synthville ; "
        f"Contact {identity}{separator}Énergie : capacité annoncée."
    )
    with db.transaction():
        for filename, text, fields in (
            ("boundary-cerfa.pdf", FILLER, {"denomination": "EXAMPLEONENERGIE"}),
            ("boundary-decision.pdf", FILLER + decision, None),
        ):
            ingest_pdf(
                db,
                blobs,
                data=make_pdf([text], fields),
                source=SourceKind.DREAL_CASE_BY_CASE,
                region="hauts-de-france",
                number="privacy-boundary-synthetic",
                url=f"https://example.org/{filename}",
                title="Projet de stockage par batteries",
                filename=filename,
            )
    classifier = RecordingClassifier()
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, lookups=lookups))
    assert classifier.inputs
    assert all(identity.lower() not in (item.title + item.text).lower() for item in classifier.inputs)
    filing = db.filing("dreal_case_by_case:hauts-de-france:privacy-boundary-synthetic")
    assert filing is not None and filing.developer == "EXAMPLEONENERGIE"
    assert identity.lower() not in filing.model_dump_json().lower()
    assert audit_privacy(db, blobs) == []


def test_short_identity_matching_never_globally_exempts_or_masks_short_parts() -> None:
    scrubber = PersonScrubber.for_filing([], people=["Li Wu"])
    assert scrubber.scrub("Li ; secteur ; Wu ; LIWUFLOW ; XLiWu") == "Li ; secteur ; Wu ; LIWUFLOW ; XLiWu"


@pytest.mark.parametrize("company", ["« Li Wu »", '"Li Wu"', "(Li Wu)", "«LiWu»", "'LI_WU'"])
def test_personal_identity_wrappers_do_not_create_a_company_exemption(company: str) -> None:
    scrubber = PersonScrubber.for_filing([], [company], people=["Li Wu"])
    assert not scrubber.keep
    assert scrubber.scrub("Contact : LiWu") == f"Contact : {MASK}"
    assert scrubber.leaks(scrubber.scrub(company) or "") == []


@pytest.mark.parametrize("first", ["Ab", "Abe"])
def test_short_person_applicant_does_not_leak_from_clause_or_candidate(
    db: Database, blobs: BlobStore, lookups: StaticLookups, first: str
) -> None:
    class SyntheticReader(PdfReader):
        def __init__(self, contents: dict[str, PdfContent]) -> None:
            super().__init__()
            self.contents = contents

        def read(self, document_id: str, data: bytes) -> PdfContent:
            return self.contents[document_id]

    class RecordingClassifier:
        def __init__(self) -> None:
            self.inputs: list[ClassificationInput] = []

        def classify(self, item: ClassificationInput) -> Classification:
            self.inputs.append(item)
            return Classification(category=Category.BESS, method="synthetic-short-person")

    # Privacy-only capsule: invented identity and composed administrative clause.
    # A deterministic reader supplies the text; no native PDF/OCR or model runs.
    applicant = f"{first} Exampleon"
    key = "dreal_case_by_case:synthetic:short-person"
    clause = (
        "Vu le formulaire d'examen au cas par cas n° 999999, déposé complet le 05 janvier 2026, "
        f"par {applicant} relatif au projet de stockage par batteries, sur la commune de Synthville ;"
    )
    contents: dict[str, PdfContent] = {}
    documents = []
    for role, text in ((DocumentRole.FORM, "Formulaire de demande."), (DocumentRole.DECISION, clause)):
        document_id = blobs.put(text.encode())
        contents[document_id] = PdfContent(pages=1, fields={}, page_texts=(text,), scanned_pages=())
        documents.append(
            DocumentRecord(
                id=document_id,
                source=SourceKind.DREAL_CASE_BY_CASE,
                url=f"https://example.org/{role.value}.pdf",
                title="Projet de stockage par batteries",
                role=role,
                filing_key=key,
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                filename=f"{role.value}.pdf",
                pages=1,
            )
        )
    reader, classifier = SyntheticReader(contents), RecordingClassifier()
    filing = build_filing(
        key,
        documents,
        Services(blobs=blobs, ocr=NullOcr(), classifier=classifier, lookups=lookups, reader=reader),
    )
    assert filing.developer == MASKED_PERSON
    assert set(filing.evidence) >= {"number", "description", "filed_on"}
    assert filing.candidates["developer"][0].value == MASK
    for name in ("number", "description", "filed_on"):
        assert applicant not in filing.evidence[name].quote
        assert MASK in filing.evidence[name].quote
    assert classifier.inputs
    assert all("exampleon" not in (item.title + item.text).lower() for item in classifier.inputs)
    assert "exampleon" not in filing.model_dump_json().lower()
    with db.transaction():
        for document in documents:
            db.upsert_document(document)
        db.upsert_filing(filing)
    assert audit_privacy(db, blobs, reader) == []
