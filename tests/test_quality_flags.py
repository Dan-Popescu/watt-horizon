"""Doubt detectors and the audit policy: each flag fires on its case and stays quiet otherwise."""

from datetime import date

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, CheckStatus, DocumentRole, SourceKind
from radar.domain.models import Check, Classification, Evidence
from radar.extraction.cerfa import form_layout
from radar.extraction.facts import Facts
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.quality.audit import decide, near_miss, stable_fraction
from radar.quality.flags import QualityInput, ReadDocument, detect, ocr_noise, same_name
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf

BESS = Classification(category=Category.BESS, method="gold")


def quality(**kw: object) -> QualityInput:
    base: dict[str, object] = {
        "documents": [ReadDocument("d1", DocumentRole.DECISION, "texte du document", {}, None)],
        "form": Facts(),
        "decision": Facts(),
        "facts": Facts(developer="APAL MW", commune="Aubenton", mw_max=100.0, filed_on=date(2026, 1, 5)),
        "classification": BESS,
        "checks": (),
        "target": True,
        "kept_by_text": True,
        "unread": [],
    }
    return QualityInput(**{**base, **kw})  # type: ignore[arg-type]


def codes(q: QualityInput) -> set[tuple[str, str | None]]:
    return {(f.code, f.field) for f in detect(q)}


def test_clean_filing_has_no_flag() -> None:
    assert codes(quality()) == set()


def test_reading_flags() -> None:
    garbage = "l1 ;: ,, I|| ~~ ¤¤ 0O0 ‰‰ ^^ projet"
    doc = ReadDocument(
        "d1", DocumentRole.DECISION, "t", {}, None, ocr_texts={1: garbage}, ocr_engines={1: "tesseract-5"}
    )
    unknown_form = ReadDocument("d2", DocumentRole.FORM, "t", {"champInconnu": "x"}, "unknown")
    found = codes(quality(documents=[doc, unknown_form], unread=["d3"]))
    assert {("ocr_noisy", None), ("form_layout_unknown", None), ("unread_scan", None)} <= found
    assert ocr_noise("Vu le formulaire d’examen n° 2025-4009, déposé le 17 novembre 2025.") < 0.1


def test_quote_must_be_in_the_document() -> None:
    facts = Facts(developer="APAL MW", commune="Aubenton", mw_max=100.0, filed_on=date(2026, 1, 5))
    facts.evidence["developer"] = Evidence(document_id="d1", locator="text", quote="citation absente")
    facts.evidence["commune"] = Evidence(document_id="d1", locator="field:LocalityNameShared", quote="Aubenton")
    assert codes(quality(facts=facts)) == {("quote_not_found", "developer")}  # field quotes are not text quotes


def test_disagreement_is_tolerant() -> None:
    assert same_name("POSTE ELECTRIQUE DU CHEMIN DE VAILLY 2", "SAS poste électrique du chemin de Vailly")
    assert same_name("Loon-Plage", "Loon-Plage et Gravelines")
    assert not same_name("APAL MW", "DECADE ENERGY ASSETS")
    form = Facts(developer="APAL MW", mw_max=100.0, filed_on=date(2026, 1, 5))
    decision = Facts(developer="DECADE ENERGY ASSETS", mw_max=47.0, filed_on=date(2026, 1, 5))
    assert {("disagreement", "developer"), ("disagreement", "power")} <= codes(quality(form=form, decision=decision))


def test_gaps_on_target_only() -> None:
    text = "stockage de 30 MW, SIRET 824 972 772 00027"
    docs = [ReadDocument("d1", DocumentRole.DECISION, text, {}, None)]
    found = codes(quality(documents=docs, facts=Facts(developer="X", commune="Y", filed_on=date(2026, 1, 1))))
    assert {("missing", "mw_max"), ("mentioned_not_extracted", "power"), ("mentioned_not_extracted", "siret")} <= found
    assert codes(quality(documents=docs, facts=Facts(), target=False)) == set()


def test_decision_flags() -> None:
    checks = (Check(name="gps", status=CheckStatus.ALERT, detail="ailleurs"),)
    undecided = Classification(category=Category.UNCLASSIFIED, method="rules", quote_verified=False)
    assert {("check_alert", "gps"), ("category_undecided", None), ("classification_unverified", None)} <= codes(
        quality(checks=checks, classification=undecided, target=False)
    )
    audited = [ReadDocument("d1", DocumentRole.DECISION, "t", {}, None, audit="random")]
    assert {("audit_sample", None), ("rescued_by_text", None)} <= codes(quality(documents=audited, kept_by_text=True))
    assert ("rescued_by_text", None) not in codes(quality(documents=audited, kept_by_text=False))


def test_form_layout() -> None:
    assert form_layout({}) == "flattened"
    assert form_layout({"denomination": "APAL MW"}) == "camelcase"
    assert form_layout({"2.2. Denomination": "European Energy"}) == "numbered"
    assert form_layout({"nouveauChamp": "x"}) == "unknown"


def test_audit_policy() -> None:
    assert decide("Décision … centrale de stockage d’énergie par batteries", "u1") == (True, None)
    assert decide("Décision … projet de centrale solaire agrivoltaïque sur la commune de (…)", "u2") == (
        False,
        "near-miss:solar-wind",
    )
    assert near_miss("projet de lotissement de 12 lots à Arras") is None
    assert stable_fraction("same-url") == stable_fraction("same-url")  # reproducible sample
    sampled = sum(decide("projet de lotissement", f"https://x/{i}")[1] == "random" for i in range(2000))
    assert 30 <= sampled <= 90  # about 3 %


def test_audited_document_rescued_by_its_text(db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    """The title says nothing; the text mentions batteries: the screen missed it, the flag must say so."""
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER + " Le projet comprend un stockage d’énergie par batteries de 20 MW."]),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="000003",
            url="https://example.org/a.pdf",
            title="Décision relative au projet de centrale solaire sur la commune de (…)",
            filename="000003-decision.pdf",
            audit="near-miss:solar-wind",
        )
    gold = GoldClassifier(
        {"000003": GoldLabel("000003", Category.BESS_HYBRID)}, known_keys=db.filing_keys_with_documents()
    )
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups))
    filing = db.filing("dreal_case_by_case:hauts-de-france:000003")
    assert filing is not None
    assert {"audit_sample", "rescued_by_text"} <= {f.code for f in filing.flags}
