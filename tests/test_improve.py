"""From a bad example to a fix: locate the verified value, induce validated rule candidates, write a ticket,
and keep the located passage as evidence of the human correction."""

from datetime import UTC, datetime
from pathlib import Path

from radar.domain.models import Correction
from radar.pipeline.corrections import apply_corrections, with_machine
from radar.quality.cases import Case, CaseDocument, check
from radar.quality.improve import render, tickets, write
from radar.quality.locate import fold_keep, locate
from radar.quality.suggest import suggest
from radar.store.db import Database


def case(number: str, text: str, substation: str | None) -> Case:
    doc = CaseDocument(id=f"d{number}", role="decision", title="Décision", text=text, fields={})
    return Case(number=number, expected={"substation": substation}, documents=[doc])


def test_locate_values_as_written() -> None:
    texts = {
        "d": "déposé complet le 18 février 2025 ; 47,5 MW / 1 292 MWh ; SIRET 824 972 772 00027 ; poste d’Aubenton"
    }
    assert locate("filed_on", "2025-02-18", texts)[0].text == "18 février 2025"
    assert locate("power_mw", 47.5, texts)[0].text == "47,5" and locate("power_mw", 47.0, texts) == []
    assert locate("energy_mwh", 1292.0, texts)[0].text == "1 292"
    assert locate("siret", "82497277200027", texts)[0].left.endswith("SIRET ")
    assert locate("substation", "Aubenton", texts)[0].left.endswith("poste d’")
    assert fold_keep("Famars (59), 47,5 MW") == "famars (59), 47,5 mw"


CASES = [
    case("1", "Le projet sera raccordé au poste HTB de Vauxrot, situé à 2 km.", "Vauxrot"),
    case("2", "Raccordement prévu au poste HTB de Margival ; travaux en 2027.", "Margival"),
    case("3", "Aucun poste n'est mentionné dans ce dossier.", None),
]


def test_suggestions_fix_failures_without_inventing() -> None:
    assert not any(check(c)["substation"] for c in CASES[:2])  # the current rules miss « poste HTB de X »
    [best, *_] = suggest(CASES, "substation")
    assert best.fixes == ["1", "2"] and best.spurious == 0 and best.precision == 1.0
    assert "htb" in best.regex


def test_ticket_is_agent_ready(tmp_path: Path) -> None:
    [ticket] = tickets(CASES)
    assert ticket.field == "substation" and [f.number for f in ticket.failures] == ["1", "2"]
    assert "[Vauxrot]" in ticket.failures[0].context
    text = render(ticket)
    for needle in ("Failing cases", "Rule candidates", "extraction/substation.py", "radar cases check --strict"):
        assert needle in text
    [path] = write([ticket], tmp_path)
    assert path.name == "substation.md" and path.read_text(encoding="utf-8") == text


def test_human_value_gets_the_document_passage_as_evidence(loaded_db: Database) -> None:
    filing = loaded_db.filing("dreal_case_by_case:hauts-de-france:010504")
    assert filing is not None
    doc_id = filing.document_ids[0]
    texts = {doc_id: "… au plus proche du poste RTE « Les Hoquins Nord », en cours de construction …"}
    fix = Correction(
        id=7, filing_key=filing.key, field="substation", value="Les Hoquins Nord", author="Dan",
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
    )  # fmt: skip
    corrected = apply_corrections(with_machine(filing), [fix], texts=texts)
    evidence = corrected.evidence["substation"]
    assert evidence.document_id == doc_id and evidence.locator == "correction:7:located"
    assert "Les Hoquins Nord" in evidence.quote
