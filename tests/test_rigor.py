"""Honest measurement: intervals, stable split, strata, calibration, blind accuracy, recall estimate."""

from datetime import UTC, datetime

from radar.api.review import submit_review
from radar.domain.enums import Category, Confidence, Severity, SourceKind
from radar.domain.models import Classification, Correction, Filing, Flag
from radar.evaluation import rigor
from radar.evaluation.gold import FieldGold
from radar.evaluation.stats import split_of, wilson
from radar.pipeline.corrections import with_machine
from radar.store.db import Database

AUBENTON = "dreal_case_by_case:hauts-de-france:010504"


def test_wilson_and_split() -> None:
    assert wilson(8, 10) == (0.49, 0.943)  # 80 % on 10 items is really "49 to 94 %"
    assert wilson(0, 0) is None
    low, high = wilson(800, 1000) or (0, 0)
    assert high - low < 0.06
    assert split_of("010504") == split_of("010504") and {split_of(str(i)) for i in range(50)} == {"dev", "test"}


def test_strata_calibration_blind(loaded_db: Database) -> None:
    filings = loaded_db.filings()
    gold = {"010504": FieldGold("010504", {"power_mw": 100.0, "substation": "Autre"}, "Dan", verified=True)}
    strata = rigor.by_stratum(filings, gold)
    assert strata["form_layout"]["camelcase"] == {"rate": 0.5, "ci95": wilson(1, 2), "n": 2}

    submit_review(
        loaded_db,
        AUBENTON,
        author="Dan",
        changes={"category": "bess", "power_mw": "100", "substation": "Autre"},
        validate=True,
        blind=True,
        now=datetime(2026, 10, 12, tzinfo=UTC),
    )
    blind = rigor.blind_accuracy(loaded_db.filings(), loaded_db.corrections())
    assert blind["power_mw"]["rate"] == 1.0 and blind["substation"]["rate"] == 0.0 and blind["category"]["n"] == 1
    calibration = rigor.calibration(loaded_db.filings(), {"010504"}, {"010504"})
    assert calibration == {"high": {"rate": 1.0, "ci95": wilson(1, 1), "n": 1}}  # a "high" filing was wrong: alarm


def _filing(number: str, category: Category, *flags: Flag) -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}", source=SourceKind.DREAL_CASE_BY_CASE, region="h", number=number, title="t",
            document_ids=(), classification=Classification(category=category, method="human"),
            confidence=Confidence.HIGH, flags=flags,
        )
    )  # fmt: skip


def test_recall_estimate_from_random_audit() -> None:
    audit = Flag(code="audit_sample", severity=Severity.INFO, detail="random")
    filings = [_filing(f"k{i}", Category.BESS) for i in range(9)]
    filings += [_filing("a1", Category.BESS, audit)] + [_filing(f"a{i}", Category.OTHER, audit) for i in range(2, 11)]
    reviewed_at = datetime(2026, 1, 1, tzinfo=UTC)
    corrections = [
        Correction(filing_key=f.key, field="validated", value=None, author="Dan", created_at=reviewed_at)
        for f in filings
        if f.number.startswith("a")
    ]
    est = rigor.recall_estimate(filings, corrections, {"rejected": 100})
    # 1 target among 10 random audits of 100 rejected -> ~10 missed; 9 kept -> recall ~ 9 / 19
    assert est["targets_found"] == 1 and abs(est["recall"] - 9 / 19) < 1e-9
    low, high = est["recall_ci95"]
    assert low < est["recall"] < high
    assert rigor.recall_estimate(filings, [], {})["recall"] is None
