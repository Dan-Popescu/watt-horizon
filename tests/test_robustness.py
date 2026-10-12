"""Metamorphic robustness: realistic text perturbations must not change extracted values."""

import random

from radar.domain.enums import DocumentRole
from radar.extraction.decision import extract_decision
from radar.extraction.substation import find_substation
from radar.quality.robustness import PERTURBATIONS, Document, check_robustness
from radar.reading.pdf import dehyphenate

CLAUSE = (
    "Vu le formulaire d’examen au cas par cas n° 2025-8646, déposé complet le 18 février 2025, par la société CS 116 "
    "relatif au projet de stockage d’électricité par batteries, sur la commune de Bonningues-lès-Calais, dans le "
    "département du Pas-de-Calais ; Article 1er : Le projet n’est pas soumis à étude d’impact. Il est raccordé au "
    "poste électrique existant des Mandarins."
)


def test_perturbations_are_deterministic_and_meaning_preserving() -> None:
    for name, perturb in PERTURBATIONS.items():
        a, b = perturb(CLAUSE, random.Random(1)), perturb(CLAUSE, random.Random(1))
        assert a == b, name
    assert " " in PERTURBATIONS["nbsp_numbers"]("au 47 MW", random.Random(0)) or " " in PERTURBATIONS["nbsp_numbers"](
        "au 47 MW", random.Random(0)
    )


def test_dehyphenation() -> None:
    assert dehyphenate("installa-\ntion") == "installation"
    assert dehyphenate("Loon-\nPlage") == "Loon-Plage"
    assert dehyphenate("Bonningues-\nlès-Calais") == "Bonningues-lès-Calais"  # caught by radar shadow
    assert dehyphenate("Courcelles-\nsur-Vesle") == "Courcelles-sur-Vesle"


def test_decision_rules_tolerate_lost_accents() -> None:
    plain = PERTURBATIONS["no_accents"](CLAUSE, random.Random(0))
    facts = extract_decision("d", plain)
    assert facts.developer == "CS 116" and facts.filed_on is not None and facts.filed_on.month == 2


def test_substation_in_uppercase_documents() -> None:
    assert find_substation("RACCORDE AU POSTE SOURCE DE LA CAPELLE.")[0] == "LA CAPELLE"  # type: ignore[index]
    assert (
        find_substation("CREATION D'UN POSTE ELECTRIQUE DE TRANSFORMATION 225 KV") is None
        or find_substation("CREATION D'UN POSTE ELECTRIQUE DE TRANSFORMATION 225 KV")[0] != "TRANSFORMATION"
    )  # type: ignore[index]
    assert find_substation("POSTE ELECTRIQUE EXISTANT DES MANDARINS EN AMONT D'UNE UNITE")[0] == "MANDARINS"  # type: ignore[index]


def test_corpus_check_reports_stability() -> None:
    doc = Document("d", DocumentRole.DECISION, "Décision", CLAUSE, {})
    report = check_robustness([("2025-8646", [doc])])
    assert report.stable_share == 1.0 and not report.fragile
    assert set(report.checks) == set(PERTURBATIONS)
