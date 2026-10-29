"""Supplementary project statements and bounded retrieval throughout long inputs."""

from radar.domain.enums import DocumentRole
from radar.extraction.llm import plan_passages, select_passages
from radar.extraction.supporting import extract_supporting
from radar.pipeline.filings import extract_facts
from radar.reading.pdf import PdfContent


def content(*pages: str) -> PdfContent:
    return PdfContent(len(pages), {}, pages, ())


def test_opinion_and_annex_quantities_have_page_evidence() -> None:
    # Constructed example of an explicit project statement; not human gold.
    text = "Le projet de stockage par batteries prévoit une puissance de 30 MW et une capacité de 60 MWh."
    result = extract_facts([("opinion", DocumentRole.OPINION, "Avis")], [content("Préambule", text)])
    assert (result.facts.mw_max, result.facts.mwh) == (30, 60)
    assert result.facts.evidence["power"].locator == "page:2"
    assert result.facts.evidence["power"].quote == text
    assert result.candidates["power_mw"][0].source == "supporting:page"


def test_supporting_contexts_abstain_on_ambiguity_and_conflicts() -> None:
    for text in (
        "Le projet de stockage prévoit 30 MW ; le projet de stockage prévoit 45 MW.",
        "Le projet de stockage existant présente une puissance de 30 MW.",
        "Le projet de stockage comporte une phase 1 de 30 MW.",
        "Le projet de stockage est raccordé à un transformateur de 30 MW.",
        "Le projet photovoltaïque prévoit une puissance de 30 MW et un stockage de 60 MWh.",
        "Le projet de stockage prévoit 30 MWdc.",
    ):
        result = extract_supporting([("doc", content(text))])
        assert result.mw_max is None and result.mwh is None, text


def test_supporting_never_overwrites_primary_statement_or_joins_unproven_pair() -> None:
    decision = "Le projet a une puissance de 40 MW."
    annex = "Le projet de stockage par batteries prévoit une puissance de 30 MW et une capacité de 60 MWh."
    result = extract_facts(
        [("decision", DocumentRole.DECISION, "Décision"), ("annex", DocumentRole.OTHER, "Annexe")],
        [content(decision), content(annex)],
    )
    assert result.facts.mw_max == 40 and result.facts.mwh is None
    # Separate citations cannot be combined into a pair with a single evidence entry.
    assert (
        extract_supporting(
            [
                ("a", content("Le projet de stockage prévoit 30 MW.")),
                ("b", content("Le projet de stockage prévoit 60 MWh.")),
            ]
        ).mw_max
        is None
    )


def test_supporting_normalizes_units_and_keeps_range() -> None:
    facts = extract_supporting([("a", content("Le projet de stockage prévoit 250 à 500 kW et 1000 kWh."))])
    assert (facts.mw_min, facts.mw_max, facts.mwh) == (0.25, 0.5, 1)


def test_long_document_selection_reaches_late_fields_despite_early_repetition() -> None:
    text = ("puissance de 20 MW " + "préambule " * 100) * 150
    text += " La société ANNEXE SAS, SIRET 82497277200027. Commune de Famars. Capacité 60 MWh. "
    selected = plan_passages(text, ("power_mw", "developer", "siret", "commune", "energy_mwh"), budget=4000)
    assert "ANNEXE SAS" in selected.text and "60 MWh" in selected.text
    assert len(selected.text) <= 4000 and selected.omitted_chars > 0
    assert selected.covered_clues < selected.matched_clues
    assert all(text[a:b] in selected.text for a, b in selected.ranges)


def test_passage_power_does_not_match_energy_only() -> None:
    text = "x" * 3000 + " capacité 60 MWh " + "y" * 3000
    selected = plan_passages(text, ("power_mw",), budget=1000)
    assert selected.matched_clues == 0
    assert select_passages(text, ("power_mw",), budget=1000) == text[:1000]
