from datetime import date

from radar.classification.base import ChainClassifier, ClassificationInput
from radar.classification.gold import GoldClassifier, GoldLabel, load_gold
from radar.classification.rules import screen
from radar.config import Settings
from radar.domain.enums import Category, CheckStatus, Confidence, SourceKind
from radar.domain.models import Filing, Location, Signal
from radar.extraction.facts import Facts
from radar.linking.linker import propose_links
from radar.validation.checks import MASKED_PERSON, check_dates, check_power, check_siret, luhn_ok
from radar.validation.lookups import CompanyInfo, StaticLookups
from radar.validation.validator import validate
from radar.wiring import make_classifier
from tests.conftest import BONNEUIL


def test_screen_recall_on_truncated_titles() -> None:
    assert screen(
        "Formulaire de demande d’examen au cas par cas, relatif au projet de Stockage électricité par (…)"
    ).keep
    assert screen(
        "Décision de non soumission … pour l’installation d’un datacenter, sur le site SIG de Béthune (62)"
    ).keep
    assert screen("création d’un poste 225 000/33 000 volts sur la commune de Courcelles-sur-Vesle (02)").keep
    # Battery waste passes layer 1 on purpose (recall first); layer 2 must reject it.
    assert screen("augmentation de la capacité de stockage de batteries usagées au plomb").keep
    assert not screen("Projet de centrale photovoltaïque au sol sur la commune de Nampont-Saint-Martin (80)").keep


def test_gold_and_chain(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "gold.jsonl"
    path.write_text('{"number": "2025-8646", "category": "bess", "note": "CS 116"}\n', encoding="utf-8")
    labels = load_gold(path)
    chain = ChainClassifier(GoldClassifier(labels))
    item = ClassificationInput(filing_key="k", number="2025-8646", title="t", text="x")
    result = chain.classify(item)
    assert result is not None and result.category is Category.BESS and result.method == "gold"
    assert chain.classify(ClassificationInput(filing_key="k", number="nope", title="t", text="x")) is None
    assert GoldLabel("1", Category.OTHER).category is Category.OTHER


def test_luhn() -> None:
    assert luhn_ok("82497277200027")
    assert not luhn_ok("82497277200028")


def test_validation_detects_wrong_gps_and_verifies_siret(lookups: StaticLookups) -> None:
    facts = Facts(
        developer="APAL MW",
        siret="82497277200027",
        commune="Bonneuil-les-Eaux",
        department="60",
        lat=49.84961,
        lon=2.24839,
        mw_max=47,
        mwh=94,
        filed_on=date(2026, 2, 11),
    )
    result = validate(facts, lookups)
    by_name = {c.name: c for c in result.checks}
    assert by_name["siret"].status is CheckStatus.OK
    assert by_name["commune"].status is CheckStatus.OK and result.commune == BONNEUIL
    assert by_name["gps"].status is CheckStatus.ALERT and "Vers-sur-Selle" in by_name["gps"].detail
    assert result.confidence(needs_review=False) is Confidence.REVIEW


def test_gdpr_masking_and_company_names(lookups: StaticLookups) -> None:
    person = Facts(developer="Pierre-Alexandre Cichostepski")
    validate(person, lookups)
    assert person.developer == MASKED_PERSON
    company = Facts(developer="European Energy")
    validate(company, lookups)
    assert company.developer == "European Energy"


def test_power_and_date_checks() -> None:
    assert check_power(Facts(mw_max=100, mwh=2000)).status is CheckStatus.ALERT
    assert check_power(Facts()).status is CheckStatus.UNKNOWN
    assert check_dates(Facts(filed_on=date(2031, 1, 1)), today=date(2026, 10, 12)).status is CheckStatus.ALERT


def test_linker_rules() -> None:
    a = Filing(
        key="dreal:hdf:005382",
        source=SourceKind.DREAL_CASE_BY_CASE,
        region="hdf",
        number="005382",
        title="",
        document_ids=(),
        developer="DECADE ENERGY ASSETS",
        location=Location(commune="Méru"),
    )
    b = Signal(
        key="company:1",
        kind="company",
        source=SourceKind.COMPANY_REGISTRY,
        title="DECADE STORAGE ASSETS 1",
        url="u",
        developer="DECADE STORAGE ASSETS 1",
    )
    c = Signal(key="article:1", kind="article", source=SourceKind.PRESS, title="x", url="u", communes=("Méru",))
    d = Signal(key="article:2", kind="article", source=SourceKind.PRESS, title="y", url="u", communes=("Méru",))
    links = {(lk.a, lk.b): lk.reasons for lk in propose_links([a], [b, c, d])}
    assert any("decade" in r for r in links[("company:1", "dreal:hdf:005382")])
    assert any("meru" in r for r in links[("article:1", "dreal:hdf:005382")])
    assert ("article:1", "article:2") not in links  # press-to-press links are skipped


def test_offline_disables_llm(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        data_dir=tmp_path,
        offline=True,
        llm_model="claude-opus-5-5",
        use_tesseract=False,
        user_agent="t",
        gold_dir=tmp_path / "gold",
    )
    assert make_classifier(settings) is None  # no LLM offline, and no gold file yet


def test_siret_name_ignores_spacing() -> None:
    lookups = StaticLookups(
        companies={
            "921873378": CompanyInfo(siren="921873378", name="AFR 59", naf=None, created_on=None, legal_form=None)
        }
    )
    check = check_siret(Facts(developer="AFR59", siret="92187337800019"), lookups)
    assert check.status is CheckStatus.OK, check.detail
