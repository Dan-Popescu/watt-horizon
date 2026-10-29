"""Human observations remain stable while extraction and review state evolve."""

import json
from datetime import UTC, date, datetime

from radar.classification.bayes import trusted_examples
from radar.domain.enums import Category, Confidence, PowerStatus, Severity, SourceKind
from radar.domain.models import Candidate, Classification, Correction, Filing, Flag, Power
from radar.pipeline.corrections import apply_corrections, reviewed_references, with_machine
from radar.quality.arbiter import latest, learn, verified_values
from radar.quality.audit import stable_fraction
from radar.quality.confidence import calibrate, field_observations
from radar.quality.errors import analyse
from radar.quality.propagate import learn_aliases
from radar.quality.triage import triage

AT = datetime(2026, 10, 29, tzinfo=UTC)


def filing(number: str = "observed") -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="h",
            number=number,
            title="Synthetic battery filing",
            document_ids=(),
            classification=Classification(category=Category.BESS, method="rules"),
            power=Power(mw_max=50.0, status=PowerStatus.KNOWN),
            strata={"evaluation_partition": "dev"},
        )
    )


def correction(f: Filing, field: str, value: str | float | None) -> Correction:
    return Correction(filing_key=f.key, field=field, value=value, author="reviewer", created_at=AT)


def test_validation_snapshot_never_adopts_rebuilt_machine_values() -> None:
    observed = filing()
    assert observed.machine is not None
    approved = correction(observed, "validated", json.dumps(observed.machine.values))
    rebuilt = with_machine(observed.model_copy(update={"power": Power(mw_max=80.0, status=PowerStatus.KNOWN)}))
    _, references = reviewed_references([rebuilt], [approved])
    assert references[rebuilt.number].expected["power_mw"] == 50.0
    assert verified_values(rebuilt, latest([approved]))["power_mw"] == 50.0


def test_legacy_validation_without_snapshot_does_not_create_truth() -> None:
    observed = filing()
    legacy = correction(observed, "validated", None)
    explicit = correction(observed, "power_mw", 60.0)
    labels, references = reviewed_references([observed], [legacy, explicit])
    assert not labels
    assert references[observed.number].expected == {"power_mw": 60.0}
    assert verified_values(observed, latest([legacy])) == {}


def test_partial_review_keeps_remaining_fields_in_queue() -> None:
    observed = filing()
    queue = triage([observed], [correction(observed, "power_mw", 50.0)], date(2026, 10, 29))
    assert len(queue) == 1
    assert "power_mw" not in queue[0].pending_fields
    assert "category" in queue[0].pending_fields


def test_legacy_validation_returns_for_complete_review() -> None:
    observed = filing()
    queue = triage([observed], [correction(observed, "validated", None)], date(2026, 10, 29))
    assert len(queue) == 1 and "power_mw" in queue[0].pending_fields


def test_partial_validation_snapshot_does_not_raise_whole_filing_confidence() -> None:
    observed = filing()
    partial = correction(observed, "validated", json.dumps({"power_mw": 50.0}))
    assert apply_corrections(observed, [partial]).confidence is Confidence.REVIEW


def test_calibration_cannot_borrow_correct_absences_from_another_field() -> None:
    reviewed = [
        filing(str(i)).model_copy(
            update={
                "strata": {"evaluation_partition": "calibration"},
                "field_score": {"developer": 0.97, "power_mw": 0.97},
            }
        )
        for i in range(100)
    ]
    decisions = [correction(f, "developer", None) for f in reviewed]
    decisions.append(correction(reviewed[0], "power_mw", 60.0))
    learned = calibrate(reviewed, decisions)
    assert learned.auto_accepted(0.97, field_name="developer", present=False)
    assert not learned.auto_accepted(0.97, field_name="power_mw", present=True)
    assert not learned.auto_accepted(0.97, field_name="developer", present=True)
    assert not learned.auto_accepted(0.97)


def test_test_annotations_never_train_arbitration_or_calibration() -> None:
    held_out = filing().model_copy(
        update={
            "strata": {"evaluation_partition": "test"},
            "field_score": {"power_mw": 0.97},
            "candidates": {"power_mw": [Candidate(source="form:field", value=50.0)]},
        }
    )
    approved = correction(held_out, "power_mw", 50.0)
    assert not learn([held_out], [approved]).stats
    assert not calibrate([held_out], [approved]).auto_accepted(0.97, field_name="power_mw")
    assert len(field_observations([held_out], [approved], partition="test")) == 1


def test_held_out_errors_count_before_details_are_hidden() -> None:
    held_out = filing().model_copy(
        update={
            "strata": {"evaluation_partition": "test"},
            "flags": (Flag(code="check_alert", severity=Severity.WARNING),),
        }
    )
    report = analyse([held_out], [correction(held_out, "power_mw", 60.0)])
    assert report.with_errors == report.test_errors == 1
    assert report.flag_recall == report.flag_precision == 1.0
    assert report.by_flag["check_alert"]["with_errors"] == 1
    assert report.errors == report.unflagged == []


def test_held_out_annotations_do_not_create_aliases_or_classifier_examples() -> None:
    held_out = with_machine(
        filing().model_copy(
            update={
                "developer": "la SAS Volta",
                "vocabulary": ("batteries",),
                "strata": {"evaluation_partition": "test"},
            }
        )
    )
    assert learn_aliases([held_out], [correction(held_out, "developer", "VOLTA")]) == {}
    assert trusted_examples([held_out], {held_out.number: Category.BESS}) == []


def test_random_audit_keeps_a_sample_of_auto_accepted_values_visible() -> None:
    number = next(str(i) for i in range(1000) if stable_fraction(f"accepted:d:h:{i}") < 0.03)
    approved = filing(number).model_copy(update={"auto_accepted": ("power_mw",)})
    [item] = triage([approved], [], date(2026, 10, 29))
    assert item.audit_fields == ["power_mw"]
    assert "power_mw" in item.pending_fields
