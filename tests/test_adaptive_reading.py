"""Constructed routing/context cases exercise mechanisms, never business references."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.models import Correction, Evidence
from radar.extraction.context import quantity_issues
from radar.extraction.facts import Facts
from radar.extraction.observations import observe_quantities
from radar.pipeline.filings import Services, _locate_segments, build_filing, extract_facts
from radar.pipeline.ingest import ingest_pdf
from radar.reading import pdf
from radar.reading.formats import read_document
from radar.reading.ocr import NullOcr
from radar.reading.pdf import PdfContent, PdfReader
from radar.reading.policy import ReadingPolicy, complete_reading, text_digest
from radar.reading.secondary import SelectiveOcr
from radar.reading.segments import DocumentSegment
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf


def test_adaptive_tables_select_after_cheap_pass_and_report_budget(monkeypatch) -> None:
    calls = []

    def tables(page, **kwargs):
        calls.append(page.number + 1)
        return (), False

    monkeypatch.setattr(pdf, "extract_tables", tables)
    content = pdf._read_pdf(
        make_pdf([FILLER, FILLER + " Stockage puissance 30 MW", FILLER + " Batterie phase 1 40 MW"]),
        read_tables=True,
        policy=ReadingPolicy(mode="adaptive", max_deep_pages=1),
    )
    assert calls == [3]  # two clues beat a single clue; budget is document-wide
    assert content.table_pages == (3,) and content.useful_unread_pages == (2,)
    assert "budget" in content.table_errors[2].lower()
    assert len(content.page_texts) == 3 and content.deepening_reasons[3]


class FixedOcr:
    label = "fixed"

    def __init__(self, text: str) -> None:
        self.text = text
        self.selected: tuple[int, ...] = ()

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        self.selected = content.scanned_pages
        return replace(
            content,
            ocr_texts=content.ocr_texts | dict.fromkeys(content.scanned_pages, self.text),
            ocr_engines=content.ocr_engines | dict.fromkeys(content.scanned_pages, self.label),
        )


def test_adaptive_ocr_keeps_unvisited_pages_unknown() -> None:
    content = PdfContent(3, {}, ("", "Puissance batteries 30 MW", ""), (1, 3))
    engine = FixedOcr("Le projet de stockage prévoit 30 MW.")
    done = complete_reading("doc", b"pdf", content, engine, ReadingPolicy(mode="adaptive", max_ocr_pages=1))
    assert engine.selected == (1,)
    assert done.scanned_pages == (1, 3) and done.unread_scanned_pages == (3,)
    assert done.useful_unread_pages == (3,) and "budget" in done.ocr_errors[3].lower()


def test_first_ocr_pass_proof_survives_improvement() -> None:
    content = PdfContent(1, {}, ("",), (1,))
    primary = FixedOcr("30 MW")
    secondary = FixedOcr("Le projet de stockage prévoit une puissance de 30 MW.")
    done = SelectiveOcr(primary, secondary).complete("doc", b"pdf", content)
    assert done.ocr_texts[1] != done.initial_ocr_texts[1]
    assert done.initial_ocr_texts[1] == "30 MW"
    assert done.diagnostics()["initial_ocr"] == {"1": text_digest("30 MW")}


def test_policy_and_all_reading_limits_separate_persistent_cache(tmp_path: Path) -> None:
    cache = KeyValueCache(tmp_path / "cache.sqlite")
    data = make_pdf([FILLER])
    economical = PdfReader(cache)
    adaptive = PdfReader(cache, policy=ReadingPolicy(mode="adaptive", max_deep_pages=1))
    restricted = PdfReader(cache, max_pages=1)
    assert len({economical.version, adaptive.version, restricted.version}) == 3
    plain = economical.read("doc", data)
    rich = adaptive.read("doc", data)
    assert plain.page_texts == rich.page_texts
    assert rich.reading_policy == adaptive.policy.identity
    assert PdfReader(cache, policy=adaptive.policy).read("doc", data) == rich


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Le projet de stockage phase 2 prévoit 30 MW.", "phase_or_scenario_requires_selection"),
        ("Le stockage existant représente 30 MW.", "quantity_is_not_proposed"),
        ("Le projet de stockage remplace le stockage existant de 30 MW.", "proposed_and_existing_not_separated"),
        ("Le projet photovoltaïque prévoit 30 MWdc.", "quantity_belongs_to_another_asset"),
    ],
)
def test_scalar_context_is_checked_against_its_actual_proof(text: str, reason: str) -> None:
    facts = Facts(mw_min=30, mw_max=30, evidence={"power": Evidence(document_id="doc", locator="text", quote=text)})
    observations = observe_quantities([("doc", PdfContent(1, {}, (text,), ()))])
    assert reason in quantity_issues(facts, observations)


def test_contradictions_cannot_be_hidden_by_first_scalar() -> None:
    text = "Le projet de stockage prévoit 30 MW. Le projet de stockage prévoit 40 MW."
    observations = observe_quantities([("doc", PdfContent(1, {}, (text,), ()))])
    assert "contradictory_project_quantities" in quantity_issues(Facts(), observations)
    other = "Le projet nommé Alpha de stockage prévoit 30 MW. Le projet nommé Beta de stockage prévoit 30 MW."
    observations = observe_quantities([("doc", PdfContent(1, {}, (other,), ()))])
    assert "multiple_named_projects" in quantity_issues(Facts(), observations)


def test_office_observations_and_scalar_proofs_keep_honest_locations() -> None:
    content = PdfContent(
        0,
        {},
        (),
        (),
        format="docx",
        segments=(DocumentSegment("paragraph:2", "Le projet de stockage prévoit 30 MW et 60 MWh."),),
    )
    observations = observe_quantities([("doc", content)])
    assert observations and all(o.page is None and o.locator == "paragraph:2" for o in observations)
    facts = extract_facts([("doc", DocumentRole.DECISION, "Projet de stockage")], [content]).facts
    assert facts.mw_max == 30 and facts.evidence["power"].locator == "paragraph:2"
    uncertain = replace(
        content, segments=(DocumentSegment("sheet:1:cell:A1", "30 MW", True, "formula result missing"),)
    )
    assert uncertain.text == "" and not observe_quantities([("doc", uncertain)])[0].eligible


def test_pipeline_abstains_on_conflicting_quantities_and_preserves_human_override(
    db: Database, blobs: BlobStore
) -> None:
    documents = []
    with db.transaction():
        for power in (30, 40):
            documents.append(
                ingest_pdf(
                    db,
                    blobs,
                    data=make_pdf([FILLER + f" Le projet de stockage prévoit {power} MW."]),
                    source=SourceKind.DREAL_CASE_BY_CASE,
                    region="hauts-de-france",
                    number="adaptive-conflict",
                    url=f"https://example.org/{power}.pdf",
                    title="Projet de stockage",
                    filename=f"{power}.pdf",
                    role=DocumentRole.DECISION,
                )
            )
    key = documents[0].filing_key
    correction = Correction(
        filing_key=key, field="power_mw", value=35, author="constructed-reader", created_at=datetime.now(UTC)
    )
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=StaticLookups())
    filing = build_filing(key, documents, services, [correction])
    assert filing.machine is not None and filing.machine.values["power_mw"] is None
    assert filing.power.mw == 35
    assert any(flag.code == "quantity_context_review" for flag in filing.flags)
    assert filing.reading_diagnostics[documents[0].id]["quantity_issues"] == ["contradictory_project_quantities"]
    assert len(filing.quantity_observations) == 2


def test_cell_header_unit_inference_is_visible_but_never_promoted() -> None:
    content = PdfContent(
        0,
        {},
        (),
        (),
        format="xlsx",
        segments=(DocumentSegment("sheet:1:cell:B2", "30", header="Puissance MW", context="Stockage projet 30"),),
    )
    observation = observe_quantities([("doc", content)])[0]
    assert observation.high == 30 and observation.header_quote == "Puissance MW"
    assert observation.page is None and not observation.unit_in_cell and not observation.eligible


def test_structured_form_count_has_its_own_literal_segment_evidence() -> None:
    content = read_document(
        b"<p>Le projet consiste a construire un stockage par batteries. 36 conteneurs batteries.</p>"
    )
    facts = extract_facts([("doc", DocumentRole.FORM, "Projet stockage")], [content]).facts
    assert facts.containers == 36
    evidence = facts.evidence["containers"]
    assert evidence.document_id == "doc" and evidence.locator == "paragraph:1"
    assert "36 conteneurs" in evidence.quote
    assert any(evidence.quote in segment.text for segment in content.segments)


def test_structured_flattening_cannot_fabricate_a_count_proof_between_cells() -> None:
    content = PdfContent(
        0,
        {},
        (),
        (),
        format="xlsx",
        segments=(
            DocumentSegment("sheet:1:cell:A1", "Le projet consiste a construire un stockage par batteries."),
            DocumentSegment("sheet:1:cell:A2", "36"),
            DocumentSegment("sheet:1:cell:B2", "conteneurs batteries."),
        ),
    )
    facts = extract_facts([("doc", DocumentRole.FORM, "Projet stockage")], [content]).facts
    assert facts.containers is None and "containers" not in facts.evidence


def test_unlocatable_shared_coordinate_evidence_withdraws_both_coordinates() -> None:
    content = PdfContent(0, {}, (), (), format="html", segments=(DocumentSegment("paragraph:1", "Autre texte"),))
    facts = Facts(
        lat=49.2, lon=2.3, evidence={"coordinates": Evidence(document_id="doc", locator="text", quote="49.2, 2.3")}
    )
    result = _locate_segments(facts, content, "doc", supply_missing=True)
    assert result.lat is None and result.lon is None and "coordinates" not in result.evidence
