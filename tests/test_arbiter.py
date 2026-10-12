"""Learned arbitration: priors reproduce the hand-written precedence; agreement and human reviews change choices."""

from datetime import UTC, datetime

from radar.domain.enums import Category, SourceKind
from radar.domain.models import Candidate, Classification, Correction, Filing
from radar.pipeline.corrections import with_machine
from radar.quality.arbiter import Arbiter, learn


def pick(arbiter: Arbiter, field: str, *options: Candidate) -> str | float | None:
    chosen = arbiter.choose(field, list(options))
    return chosen[0].value if chosen else None


def test_priors_reproduce_precedence() -> None:
    a = Arbiter()
    assert (
        pick(
            a,
            "developer",
            Candidate(source="form:field", value="APAL MW"),
            Candidate(source="decision:clause", value="APAL"),
        )
        == "APAL MW"
    )
    assert (
        pick(
            a,
            "commune",
            Candidate(source="form:field", value="4 rue X Meru"),
            Candidate(source="decision:clause", value="Meru"),
        )
        == "Meru"
    )
    voltage, name = (
        Candidate(source="form:text", value="400kV", weak=True),
        Candidate(source="decision:text", value="Famars"),
    )
    assert pick(a, "substation", voltage, name) == "Famars"
    assert (
        pick(
            a,
            "substation",
            Candidate(source="form:text", value="Margot"),
            Candidate(source="decision:text", value="225kV", weak=True),
        )
        == "Margot"
    )


def test_agreeing_sources_win() -> None:
    a = Arbiter()
    alone = Candidate(source="form:text", value=47.0)
    pair = [Candidate(source="decision:clause", value=94.0), Candidate(source="llm", value=94.0)]
    chosen = a.choose("power_mw", [alone, *pair])
    assert chosen is not None and chosen[0].value == 94.0 and chosen[2] == 2  # 1 - 0.25 x 0.35 > 0.8


def _filing(number: str, form_value: str, decision_value: str) -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}", source=SourceKind.DREAL_CASE_BY_CASE, region="h", number=number, title="t",
            document_ids=(), developer=form_value, classification=Classification(category=Category.BESS, method="gold"),
            candidates={"developer": [Candidate(source="form:field", value=form_value), Candidate(source="decision:clause", value=decision_value)]},
        )
    )  # fmt: skip


def test_reviews_teach_which_source_to_trust() -> None:
    """Ten reviews say the form's denomination is a brand while the decision names the company that files."""
    filings = [_filing(str(i), f"Marque {i}", f"Societe Projet {i}") for i in range(10)]
    at = datetime(2026, 10, 12, tzinfo=UTC)
    corrections = [
        Correction(filing_key=f.key, field="developer", value=f"Societe Projet {f.number}", author="Dan", created_at=at)
        for f in filings
    ]
    learned = learn(filings, corrections)
    assert learned.stats[("developer", "form:field")] == (0, 10) and learned.stats[
        ("developer", "decision:clause")
    ] == (10, 10)
    assert (
        learned.reliability("developer", "form:field") < 0.3 < 0.9 < learned.reliability("developer", "decision:clause")
    )
    new = [
        Candidate(source="form:field", value="Marque X"),
        Candidate(source="decision:clause", value="Societe Projet X"),
    ]
    assert pick(Arbiter(), "developer", *new) == "Marque X"  # before reviews: the hand-written precedence
    assert pick(learned, "developer", *new) == "Societe Projet X"  # after: learned, no code change
    assert {row["source"] for row in learned.table()} == {"form:field", "decision:clause"}
