"""Learning from corrections: error clusters, detector effectiveness, replayable regression cases."""

from dataclasses import replace
from pathlib import Path

from radar.api.review import submit_review
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category
from radar.pipeline.corrections import reviewed_references
from radar.pipeline.filings import Services, read_documents
from radar.pipeline.run import process_filings
from radar.quality import cases
from radar.quality.errors import analyse
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups

AUBENTON = "dreal_case_by_case:hauts-de-france:010504"


def services(blobs: BlobStore, lookups: StaticLookups) -> Services:
    gold = {"010504": GoldLabel("010504", Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)}
    return Services(blobs=blobs, ocr=NullOcr(), classifier=GoldClassifier(gold), lookups=lookups)


def test_error_analysis(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    submit_review(loaded_db, AUBENTON, author="Dan", changes={"power_mw": "120"}, cause="rule", validate=True)
    process_filings(loaded_db, services(blobs, lookups), only={AUBENTON})
    development = [
        f.model_copy(update={"strata": f.strata | {"evaluation_partition": "dev"}}) for f in loaded_db.filings()
    ]
    report = analyse(development, loaded_db.corrections())
    assert report.reviewed == 1 and report.with_errors == 1
    [error] = report.errors
    assert (error.field, error.outcome, error.machine, error.human, error.cause) == (
        "power_mw",
        "wrong",
        100.0,
        120.0,
        "rule",
    )
    assert report.clusters[0]["count"] == 1 and report.clusters[0]["examples"] == ["010504"]
    assert report.unflagged == ["010504:power_mw (wrong)"]  # no detector saw it: a detector to write


def test_cases_replay_and_lifecycle(
    loaded_db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path
) -> None:
    submit_review(loaded_db, AUBENTON, author="Dan", changes={"power_mw": "120"}, cause="rule", validate=True)
    svc = services(blobs, lookups)
    process_filings(loaded_db, svc, only={AUBENTON})
    filing = loaded_db.filing(AUBENTON)
    assert filing is not None
    filing = filing.model_copy(update={"strata": filing.strata | {"evaluation_partition": "dev"}})
    _, verified = reviewed_references(loaded_db.filings(), loaded_db.corrections())
    documents = loaded_db.documents(AUBENTON)
    case = cases.make_case(filing, documents, read_documents(documents, svc), verified["010504"].expected)
    assert case.status["power_mw"] == "open" and case.status["substation"] == "fixed"  # rules say 100, human 120
    assert "department" not in case.expected  # not replayable by the rules alone
    assert cases.replay(case)["energy_mwh"] == 200.0

    cases.save(case, tmp_path)
    assert cases.load(tmp_path / "010504.json") == case
    cases.save(replace(case, expected={**case.expected, "power_mw": 100.0}), tmp_path)  # "the rules were improved"
    result = cases.refresh(tmp_path)
    assert result["newly_fixed"] == ["010504:power_mw"] and not result["regressed"]
    broken = cases.load(tmp_path / "010504.json")
    cases.save(replace(broken, expected={**broken.expected, "substation": "Autre poste"}), tmp_path)
    assert cases.refresh(tmp_path)["regressed"] == ["010504:substation"]  # a fixed field failing again
