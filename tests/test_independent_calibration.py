"""Repeated records of one project cannot prove independent calibration reliability."""

from datetime import UTC, datetime

import pytest

from radar.domain.enums import Category, PowerStatus, SourceKind
from radar.domain.models import Candidate, Classification, Correction, Filing, Power
from radar.pipeline.corrections import with_machine
from radar.quality.arbiter import learn
from radar.quality.confidence import Calibration, calibrate, field_observations, observations
from radar.quality.model_check import reliability

AT = datetime(2026, 10, 29, tzinfo=UTC)


def reviewed(
    index: int, group: str, *, value: float = 50.0, partition: str = "calibration"
) -> tuple[Filing, Correction]:
    filing = with_machine(
        Filing(
            key=f"dreal:north:{index}",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="north",
            number=str(index),
            title="Synthetic battery project",
            document_ids=(f"pdf:{group}",),
            classification=Classification(category=Category.BESS, method="rules"),
            power=Power(mw_max=value, status=PowerStatus.KNOWN),
            field_score={"power_mw": 0.97},
            model_checks={"power_mw": "agree"},
            candidates={"power_mw": [Candidate(source="form:field", value=value)]},
            strata={"evaluation_partition": partition, "evaluation_group": group},
        )
    )
    correction = Correction(filing_key=filing.key, field="power_mw", value=50.0, author="reviewer", created_at=AT)
    return filing, correction


def test_one_project_copied_one_hundred_times_does_not_prove_auto_acceptance() -> None:
    records = [reviewed(i, "one-project") for i in range(100)]
    filings, corrections = [f for f, _ in records], [c for _, c in records]
    calibrated = calibrate(filings, corrections)
    assert calibrated.by_field[("power_mw", True)][-1][2:] == (1, 1)
    assert len(observations(filings, corrections)) == 1
    assert not calibrated.auto_accepted(0.97, field_name="power_mw", present=True)


def test_one_hundred_independent_projects_can_prove_auto_acceptance() -> None:
    records = [reviewed(i, f"project-{i}") for i in range(100)]
    calibrated = calibrate([f for f, _ in records], [c for _, c in records])
    assert calibrated.by_field[("power_mw", True)][-1][2:] == (100, 100)
    assert calibrated.auto_accepted(0.97, field_name="power_mw", present=True)


def test_one_wrong_observation_makes_its_entire_group_unsuccessful() -> None:
    records = [reviewed(i, "one-project", value=80.0 if i == 99 else 50.0) for i in range(100)]
    calibrated = calibrate([f for f, _ in records], [c for _, c in records])
    assert calibrated.by_field[("power_mw", True)][-1][2:] == (0, 1)
    assert not calibrated.auto_accepted(0.97, field_name="power_mw", present=True)


def test_shared_pdf_cannot_be_counted_as_independent_groups() -> None:
    records = [reviewed(i, f"project-{i}") for i in range(100)]
    filings = [f.model_copy(update={"document_ids": ("shared-pdf",)}) for f, _ in records]
    calibrated = calibrate(filings, [c for _, c in records])
    assert calibrated.by_field[("power_mw", True)][-1][2:] == (1, 1)


def test_test_ece_weights_independent_groups_not_their_number_of_duplicates() -> None:
    records = [reviewed(i, "correct", partition="test") for i in range(100)]
    records.append(reviewed(100, "wrong", value=80.0, partition="test"))
    pairs = field_observations([f for f, _ in records], [c for _, c in records], partition="test")
    assert len(pairs) == 2
    # One bin: predicted 97%, observed one successful group out of two = 50%.
    assert Calibration().field_ece(pairs) == pytest.approx(0.47)


def test_pre_review_model_reliability_requires_independent_groups() -> None:
    records = [reviewed(i, "one-project", value=80.0 if i == 99 else 50.0) for i in range(100)]
    measured = reliability([f for f, _ in records], [c for _, c in records])
    assert (measured.agree_right, measured.agree_seen) == (0, 1)
    assert not measured.proven_for("power_mw")
    independent = [reviewed(i, f"project-{i}") for i in range(100)]
    assert reliability([f for f, _ in independent], [c for _, c in independent]).proven_for("power_mw")


def test_one_project_cannot_dominate_source_arbitration_with_copies() -> None:
    records = [reviewed(i, "one-project", value=80.0 if i == 99 else 50.0, partition="dev") for i in range(100)]
    learned = learn([f for f, _ in records], [c for _, c in records])
    assert learned.stats[("power_mw", "form:field")] == (0, 1)
