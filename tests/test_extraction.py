"""Extraction tests use verbatim excerpts of real DREAL Hauts-de-France documents (public, Licence Ouverte)."""

from datetime import date

from radar.domain.enums import ReviewDecision
from radar.extraction.cerfa import extract_cerfa
from radar.extraction.decision import extract_decision
from radar.extraction.facts import Facts
from radar.extraction.substation import find_substation
from radar.reading.pdf import PdfContent

MERU_CLAUSE = (
    "Vu le formulaire d’examen au cas par cas n° 005382 KKP, déposé complet le 8 septembre 2025, par la société "
    "DECADE Energy ASSETS relatif au projet de création d’une unité de stockage d’énergie par batteries, sur la "
    "commune de Meru, dans le département de l’Oise ; Article 1 er : Le projet de création d’une unité de stockage "
    "d’énergie par batteries sur la commune de Meru dans le département de l’Oise, déposé par la société DECADE "
    "Energy ASSETS, n’est pas soumis à étude d’impact en application de la section première du chapitre II."
)
BETHUNE_OCR = (
    "Vu le formulaire d'examen au cas par cas n°2025-4009, déposé complet le 17 novembre 2025 par la société "
    "SIG REVERSE INDUSTRY située à Béthune, concernant l'installation d'un datacenter au droit du site, pour le "
    "compte de la société AZUR DATACENTER ;"
)


def test_decision_clause() -> None:
    facts = extract_decision("doc1", MERU_CLAUSE)
    assert facts.number == "005382 KKP"
    assert facts.filed_on == date(2025, 9, 8)
    assert facts.developer == "DECADE Energy ASSETS"  # not « cas n° … » (« au cas par cas »)
    assert facts.commune == "Meru"
    assert facts.department == "60"
    assert facts.review_decision is ReviewDecision.NOT_SUBJECT
    assert facts.evidence["developer"].locator == "clause:request"
    assert "n’est pas soumis" in facts.evidence["review_decision"].quote


def test_decision_scanned_uses_title() -> None:
    title = "Décision de non soumission à la réalisation d’une étude d’impact pour l’installation d’un datacenter, sur le site SIG de Béthune (62)"
    facts = extract_decision("doc2", BETHUNE_OCR, title)
    assert facts.developer == "SIG REVERSE INDUSTRY"
    assert facts.filed_on == date(2025, 11, 17)
    assert facts.review_decision is ReviewDecision.NOT_SUBJECT
    assert facts.commune == "Béthune" and facts.department == "62"


def test_substation() -> None:
    assert find_substation("afin d'etre au plus proche du poste RTE « Margot », en cours de construction.") == (
        "Margot",
        "afin d'etre au plus proche du poste RTE « Margot », en cours de construction",
    )
    assert find_substation("raccordement depuis le poste source de SAUCOURT jusqu'au poste")[0] == "SAUCOURT"  # type: ignore[index]
    assert find_substation("L'installation sera raccordée au poste électrique RTE 400kV.")[0] == "400kV"  # type: ignore[index]
    assert find_substation("poste de livraison permettant le raccordement") is None


def _content(fields: dict[str, str], text: str = "") -> PdfContent:
    return PdfContent(pages=1, fields=fields, page_texts=(text,), scanned_pages=())


def test_cerfa_camelcase_fields() -> None:
    facts = extract_cerfa(
        "f1",
        _content(
            {
                "denomination": "APAL MW",
                "raisonsociale": "Societe par actions simplifiee",
                "numeroSiret": "82497277200027",
                "Dossiercompletdate": "11-02-2026",
                "NatureDuProjetY": "Le projet consiste a construire et a exploiter un projet de stockage par batteries de 47 MW / 94 MWh sur la commune de Bonneuil-les-Eaux. Il est situe au plus proche du poste RTE « Margot ». Il est compose de : - 16 conteneurs batteries",
                "Valeurs": "(0.85, Ha) (7403, m²) (1437, m²) (47, MW) (94, MWh) (8, m)",
                "LocalityNameShared": "Bonneuil-les-Eaux",
                "lat1Forprojectaddress": "49°50'58\"6",
                "long1ForprojectAddress": "2°14'54\"2",
            }
        ),
    )
    assert facts.developer == "APAL MW"
    assert facts.legal_name is None  # a legal form is not a company name
    assert facts.siret == "82497277200027"
    assert facts.filed_on == date(2026, 2, 11)
    assert (facts.mw_max, facts.mwh) == (47.0, 94.0)
    assert facts.evidence["power"].locator == "field:Valeurs"
    assert facts.containers == 16 and facts.area_ha == 0.85
    assert facts.substation == "Margot"
    assert facts.lat is not None and abs(facts.lat - 49.84961) < 1e-4


def test_cerfa_numbered_fields_and_locality_address() -> None:
    # Real "numbered" layout; values combined from two filings (2025-9111 company, 005554-style address).
    facts = extract_cerfa(
        "f2",
        _content(
            {
                "2.2. Denomination": "European Energy",
                "2.2. Raison sociale": "EE Agrisolaire 14",
                "2.2. SIRET": "93062604900017",
                "4.6 Localite": "Le Haut de la Gloriette 02100 Morcourt",
                "4.6 Coordonnees_Lat": "49",
                "4.6 Coordonnees_Lat °": "20",
                "4.6 Coordonnees_Lat '": "30",
                '4.6 Coordonnees_Lat "': "N",
                "4.6 Coordonnees_long": "03",
                "4.6 Coordonnees_long °": "28",
                "4.6 Coordonnees_long '": "42",
                '4.6 Coordonnees_long "': "E",
            }
        ),
    )
    assert (facts.developer, facts.legal_name, facts.siret) == (
        "European Energy",
        "EE Agrisolaire 14",
        "93062604900017",
    )
    assert (facts.commune, facts.postcode) == ("Morcourt", "02100")
    assert facts.lon is not None and abs(facts.lon - 3.47833) < 1e-4


def test_cerfa_flattened_text_fallback() -> None:
    text = (
        "Projet de stockage d'électricité par batteries à proximité du poste électrique sur la commune de Famars (59) "
        "AFR59 AFR59 JAILLON SAS 92187337800019 X 22 11 2024 2024_8433 22 11 2024 Le projet consiste à construire sur "
        "la commune de Famars, dans le département du Nord, une installation de stockage d'énergie d'une puissance "
        "de 110 MW et d'une capacité de 220 MWh. L'installation sera raccordée au poste électrique RTE 400kV."
    )
    facts = extract_cerfa("f3", _content({}, text))
    assert facts.siret == "92187337800019"
    assert facts.filed_on == date(2024, 11, 22)
    assert (facts.mw_max, facts.mwh) == (110.0, 220.0)
    assert (facts.commune, facts.department) == ("Famars", "59")
    assert facts.substation == "400kV"


def test_facts_merge_priority() -> None:
    form, decision = Facts(commune="4 Allee X 60110 Meru", developer="DECADE ENERGY ASSETS"), Facts(commune="Meru")
    merged = Facts()
    merged.fill_from(form)
    merged.fill_from(decision, override=frozenset({"commune"}))
    assert merged.commune == "Meru" and merged.developer == "DECADE ENERGY ASSETS"
