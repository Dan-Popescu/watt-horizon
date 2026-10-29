"""Stored machine classification is measured without paying to replay a classifier."""

from radar.classification.gold import GoldLabel
from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Filing
from radar.evaluation.baseline import assess_report
from radar.evaluation.classification import stored_classification_report
from radar.evaluation.gold import FieldGold
from radar.evaluation.report import ReportInputs, build_report
from radar.pipeline.corrections import with_machine


def report_for(*, category: Category, method: str, corrected: bool = False) -> dict:
    filing = with_machine(
        Filing(
            key="dreal:region:case",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="region",
            number="case",
            title="Synthetic classification",
            document_ids=(),
            classification=Classification(category=category, method=method),
            strata={"evaluation_partition": "test", "evaluation_group": "project-a"},
        )
    )
    if corrected:
        filing = filing.model_copy(update={"classification": Classification(category=Category.BESS, method="human")})
    return build_report(
        ReportInputs(
            filings=[filing],
            labels={filing.key: GoldLabel(filing.number, Category.BESS, verified=True)},
            field_gold={filing.key: FieldGold(filing.number, {"developer": None}, verified=True)},
        )
    )


def test_matching_gold_output_is_not_independent_classification_evidence() -> None:
    report = report_for(category=Category.BESS, method="gold")
    measured = report["stored_classification"]
    assert measured["evaluated"] == 1 and measured["non_independent"] == 1
    assert measured["independent_n"] == 0 and measured["independent_accuracy"] is None
    gate = assess_report(report, min_samples=1, required_fields=("developer",))
    assert "non_independent_stored_classification" in gate["failures"]


def test_perfect_fields_do_not_hide_wrong_classification() -> None:
    report = report_for(category=Category.OTHER, method="rules")
    assert report["fields_verified"]["test"]["developer"]["independent_accuracy"] == 1.0
    measured = report["stored_classification"]
    assert measured["non_independent"] == 0 and measured["independent_n"] == 1
    assert measured["independent_accuracy"] == 0.0
    gate = assess_report(report, min_samples=1, required_fields=("developer",))
    assert "stored_classification_accuracy_below_threshold" in gate["failures"]


def test_human_override_does_not_improve_measured_machine_classification() -> None:
    report = report_for(category=Category.OTHER, method="rules", corrected=True)
    assert report["stored_classification"]["correct"] == 0
    assert report["stored_classification"]["independent_accuracy"] == 0.0


def test_missing_answer_remains_a_measured_failure() -> None:
    report = report_for(category=Category.NEEDS_REVIEW, method="llm:synthetic")
    assert report["stored_classification"]["no_answer"] == 1
    assert report["stored_classification"]["independent_accuracy"] == 0.0


def test_a_group_with_one_gold_derived_output_is_not_independent() -> None:
    records = []
    for number, method in (("a", "llm:synthetic"), ("b", "gold")):
        records.append(
            with_machine(
                Filing(
                    key=f"dreal:region:{number}",
                    source=SourceKind.DREAL_CASE_BY_CASE,
                    region="region",
                    number=number,
                    title="Synthetic grouped classification",
                    document_ids=(),
                    classification=Classification(category=Category.BESS, method=method),
                    strata={"evaluation_partition": "test", "evaluation_group": "one-project"},
                )
            )
        )
    measured = stored_classification_report(
        records, {f.key: GoldLabel(f.number, Category.BESS, verified=True) for f in records}
    )
    assert measured["evaluated"] == 2 and measured["correct"] == 1
    assert measured["non_independent_groups"] == 1 and measured["independent_n"] == 0


def test_gold_outside_the_test_population_does_not_contaminate_its_measurement() -> None:
    filing = with_machine(
        Filing(
            key="dreal:region:calibration",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="region",
            number="calibration",
            title="Synthetic calibration record",
            document_ids=(),
            classification=Classification(category=Category.BESS, method="gold"),
            strata={"evaluation_partition": "calibration", "evaluation_group": "one-project"},
        )
    )
    measured = stored_classification_report(
        [filing], {filing.key: GoldLabel(filing.number, Category.BESS, verified=True)}
    )
    assert measured["evaluated"] == 0 and measured["non_independent"] == 0
