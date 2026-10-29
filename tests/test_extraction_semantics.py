"""Synthetic counterexamples exercise the public extraction verification contract."""

import pytest

from radar.extraction.llm import ExtractionInput, verify


def accepted(field: str, value: str, quote: str) -> dict[str, tuple[str | float, str]]:
    return verify({field: {"value": value, "quote": quote}}, ExtractionInput("case", "Batteries", quote, (field,)))


@pytest.mark.parametrize(
    ("field", "value", "quote"),
    [
        ("power_mw", "50", "La capacité de stockage est de 50 MWh."),
        ("energy_mwh", "50", "La puissance du projet est de 50 MW."),
        ("power_mw", "50 MWh", "La puissance du projet est de 50 MW."),
        ("power_mw", "50", "La puissance du projet est de 50 mW."),
        ("power_mw", "-50", "La puissance du projet est de 50 MW."),
        ("power_mw", "50", "Le transformateur existant présente une puissance de 50 MW."),
        ("power_mw", "50", "Un autre projet de stockage présente une puissance de 50 MW."),
        ("power_mw", "50", "La puissance de 50 MW est un exemple hypothétique."),
        ("power_mw", "70", "Le projet photovoltaïque représente 70 MWc et 50 MWac."),
        ("power_mw", "10", "La puissance du projet est comprise entre 10 et 20 MW."),
        ("power_mw_min", "20", "La puissance du projet est comprise entre 10 et 20 MW."),
        ("power_mw", "10", "La puissance du projet est comprise entre 20 et 10 MW."),
        ("filed_on", "2026-03-03", "Décision prise le 3 mars 2026."),
    ],
)
def test_rejects_wrong_dimension_role_or_context(field: str, value: str, quote: str) -> None:
    assert accepted(field, value, quote) == {}


@pytest.mark.parametrize(
    ("field", "value", "quote", "expected"),
    [
        ("power_mw", "50", "La puissance du projet est de 50 MW.", 50.0),
        ("power_mw", "0,5", "La puissance du projet est de 500 kW.", 0.5),
        ("power_mw", "500 kW", "La puissance du projet est de 500 kW.", 0.5),
        ("energy_mwh", "0,5", "La capacité de stockage est de 500 kWh.", 0.5),
        ("power_mw", "50", "Le projet photovoltaïque représente 70 MWc et 50 MWac.", 50.0),
        ("power_mw", "20", "La puissance du projet est comprise entre 10 et 20 MW.", 20.0),
        ("power_mw_min", "10", "La puissance du projet est comprise entre 10 et 20 MW.", 10.0),
        ("filed_on", "2026-03-03", "Dossier déposé complet le 3 mars 2026.", "2026-03-03"),
    ],
)
def test_keeps_grounded_normalized_values(field: str, value: str, quote: str, expected: str | float) -> None:
    assert accepted(field, value, quote) == {field: (expected, quote)}


def test_does_not_cut_the_verified_measurement_out_of_a_long_quote() -> None:
    quote = "Le projet concerne " + "un stockage par batteries " * 20 + "d'une puissance de 50 MW."
    assert accepted("power_mw", "50", quote) == {"power_mw": (50.0, quote)}


def test_short_quote_does_not_hide_the_surrounding_object_or_unit_suffix() -> None:
    answer = {"power_mw": {"value": "50", "quote": "puissance de 50 MW"}}
    for text in (
        "Un transformateur existant possède une puissance de 50 MW.",
        "Le projet photovoltaïque présente une puissance de 50 MWc.",
    ):
        assert verify(answer, ExtractionInput("k", "t", text, ("power_mw",))) == {}
