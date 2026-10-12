"""Per-field confidence: raw evidence score, calibration on reviews, cautious auto-accept, triage of uncertain fields."""

from datetime import UTC, date, datetime

from radar.domain.enums import Category, CheckStatus, PowerStatus, Severity, SourceKind
from radar.domain.models import Check, Classification, Correction, Filing, Flag, Power
from radar.pipeline.corrections import with_machine
from radar.quality.confidence import Calibration, calibrate, observations, raw_scores
from radar.quality.triage import triage


def test_raw_scores_penalise_doubt() -> None:
    values = {"developer": "APAL MW", "power_mw": 47.0, "siret": "82497277200027", "substation": None}
    support = {"developer": (0.9, 1, 1), "power_mw": (0.9, 1, 2), "siret": (0.9, 1, 1)}
    flags = [Flag(code="mentioned_not_extracted", severity=Severity.WARNING, field="substation")]
    checks = (Check(name="siret", status=CheckStatus.OK, detail="ok"),)
    raw = raw_scores(values, support, flags, checks, "text")
    assert raw["developer"] == 0.9 and raw["power_mw"] == 0.63  # sources disagree: x 0.7
    assert raw["siret"] == 0.97  # verified in the company register
    assert raw["substation"] == 0.3  # absent, yet the text mentions one
    assert raw_scores(values, support, [], (), "ocr")["developer"] == 0.81


def test_calibration_is_cautious() -> None:
    few = Calibration([(0.9, 0.95, 9, 10)])
    assert few.confidence(0.92) == round((9 + 2 * 0.925) / 12, 4) and not few.auto_accepted(0.92)  # too few
    many = Calibration([(0.95, 1.0001, 99, 100)])
    assert many.auto_accepted(0.97)
    shaky = Calibration([(0.95, 1.0001, 30, 34)])
    assert not shaky.auto_accepted(0.97)  # 88 %: below the bar
    assert Calibration().confidence(0.83) == 0.83  # nothing learned yet: raw score, no auto-accept
    assert many.ece([(0.97, True)] * 99 + [(0.97, False)]) is not None


def _filing(number: str, raw: float, machine_power: float) -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}", source=SourceKind.DREAL_CASE_BY_CASE, region="h", number=number, title="t",
            document_ids=(), classification=Classification(category=Category.BESS, method="gold"),
            power=Power(mw_max=machine_power, status=PowerStatus.KNOWN), field_score={"power_mw": raw},
            field_confidence={"power_mw": raw}, filed_on=date(2026, 9, 1),
        )
    )  # fmt: skip


def test_calibration_learns_from_reviews() -> None:
    at = datetime(2026, 10, 12, tzinfo=UTC)
    filings = [_filing(str(i), 0.92, 50.0) for i in range(4)]
    corrections = [
        Correction(filing_key=f.key, field="power_mw", value=50.0 if i < 3 else 60.0, author="Dan", created_at=at)
        for i, f in enumerate(filings)
    ]
    assert sorted(ok for _, ok in observations(filings, corrections)) == [False, True, True, True]
    learned = calibrate(filings, corrections)
    assert next(b for b in learned.bins if b[0] == 0.9)[2:] == (3, 4)


def test_uncertain_fields_rise_in_queue() -> None:
    sure, unsure = _filing("sure", 0.97, 50.0), _filing("unsure", 0.4, 50.0)
    queue = {i.number: i for i in triage([sure, unsure], [], date(2026, 10, 12))}
    assert queue["unsure"].priority > queue["sure"].priority
    assert queue["unsure"].reasons == ["champs incertains : power_mw (40%)"]
