"""Template fingerprints (stable per layout, new ones flagged) and drift alerts per source / template."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, DocumentRole, PowerStatus, Severity, SourceKind
from radar.domain.models import Classification, Filing, Flag, Power
from radar.pipeline.corrections import with_machine
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.quality.drift import drift
from radar.quality.templates import Registry, fingerprint
from radar.reading.ocr import NullOcr
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def test_fingerprint_is_stable_per_layout() -> None:
    names = ("denomination", "numeroSiret", "Valeurs")
    assert fingerprint(DocumentRole.FORM, names, "a") == fingerprint(DocumentRole.FORM, tuple(reversed(names)), "b")
    assert fingerprint(DocumentRole.FORM, names, "") != fingerprint(DocumentRole.FORM, (*names, "nouveauChamp"), "")
    decision = "Le préfet de région … Vu le formulaire d'examen … Considérant … DÉCIDE Article 1 …"
    assert fingerprint(DocumentRole.DECISION, (), decision + " avis de la commune") == fingerprint(
        DocumentRole.DECISION, (), decision
    )
    content = read_pdf(make_pdf([FILLER], {"denomination": "APAL MW", "numeroSiret": ""}))
    assert content.field_names == ("denomination", "numeroSiret")  # empty fields count for the template


def test_unknown_template_is_flagged(db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path) -> None:
    with db.transaction():
        ingest_pdf(
            db, blobs, data=make_pdf([FILLER + " stockage par batteries"], {"champInedit": "x"}),
            source=SourceKind.DREAL_CASE_BY_CASE, region="hauts-de-france", number="000009",
            url="https://example.org/f.pdf", title="Formulaire de demande d’examen au cas par cas, batteries",
            filename="000009-cerfa.pdf",
        )  # fmt: skip
    gold = GoldClassifier({"000009": GoldLabel("000009", Category.BESS)}, known_keys=db.filing_keys_with_documents())
    known = Registry({"decision:text-0": {}})
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups, templates=known))
    filing = db.filing("dreal_case_by_case:hauts-de-france:000009")
    assert filing is not None and "new_template" in {f.code for f in filing.flags}
    assert filing.strata["template"].startswith("form:fields-")
    process_filings(db, Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups))  # no registry yet
    assert "new_template" not in {f.code for f in db.filing(filing.key).flags}  # type: ignore[union-attr]


def _filing(i: int, power: float | None, doubtful: bool) -> Filing:
    flags = (Flag(code="quote_not_found", severity=Severity.ERROR),) if doubtful else ()
    return with_machine(
        Filing(
            key=f"d:h:{i}", source=SourceKind.DREAL_CASE_BY_CASE, region="h", number=str(i), title="t",
            document_ids=(), developer="X", classification=Classification(category=Category.BESS, method="gold"),
            power=Power(mw_max=power, status=PowerStatus.KNOWN) if power else Power(), flags=flags,
            strata={"template": "decision:text-abc"},
        )
    )  # fmt: skip


def test_drift_alerts_on_a_degrading_template() -> None:
    old = [_filing(i, 50.0, False) for i in range(6)]
    new = [_filing(10 + i, None, True) for i in range(6)]  # the site changed: power lost, doubt everywhere
    fetched = {f.key: NOW - timedelta(days=90) for f in old} | {f.key: NOW - timedelta(days=3) for f in new}
    alerts = {(a.group, a.value, a.metric) for a in drift(old + new, fetched, NOW)}
    assert ("template", "decision:text-abc", "completeness:power_mw") in alerts
    assert ("source", "dreal_case_by_case", "doubt_rate") in alerts
    assert drift(old, fetched, NOW) == []  # nothing recent: no comparison, no alert
