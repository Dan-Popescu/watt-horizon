"""Field accuracy against the field gold. Each comparison has one outcome:

correct  expected value found          wrong     another value extracted
missed   expected value, nothing found spurious  nothing expected (absent from documents), something extracted
absent   nothing expected, nothing found (also correct)
"""

from dataclasses import dataclass, field
from typing import Literal

from radar.domain.fields import Value
from radar.domain.models import Filing
from radar.domain.text import fold
from radar.evaluation.gold import FIELDS, FieldGold, actual_fields
from radar.evaluation.stats import wilson

Outcome = Literal["correct", "wrong", "missed", "spurious", "absent"]


def same(expected: Value, actual: Value) -> bool:
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(expected) - float(actual)) <= max(0.5, 0.01 * abs(float(expected)))
    return fold(str(expected)) == fold(str(actual))


def compare(expected: Value, actual: Value) -> Outcome:
    if expected is None:
        return "absent" if actual is None else "spurious"
    if actual is None:
        return "missed"
    return "correct" if same(expected, actual) else "wrong"


@dataclass
class FieldScore:
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    spurious: int = 0
    absent: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.missed + self.spurious + self.absent

    @property
    def accuracy(self) -> float | None:
        return (self.correct + self.absent) / self.total if self.total else None

    def as_dict(self, with_errors: bool = True) -> dict[str, object]:
        """`with_errors=False` for the test split: scores only, never the cases (no tuning on test)."""
        return {
            "accuracy": round(self.accuracy, 3) if self.accuracy is not None else None,
            "ci95": wilson(self.correct + self.absent, self.total),
            "n": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "missed": self.missed,
            "spurious": self.spurious,
            "absent": self.absent,
            **({"errors": self.errors} if with_errors else {}),
        }


def score_fields(filings: list[Filing], gold: dict[str, FieldGold], *, verified: bool) -> dict[str, FieldScore]:
    """Scores per field over gold filings with the given `verified` status."""
    by_number = {f.number: f for f in filings}
    scores = {name: FieldScore() for name in FIELDS}
    for number, reference in gold.items():
        if reference.verified is not verified:
            continue
        filing = by_number.get(number)
        actual = actual_fields(filing) if filing else dict.fromkeys(FIELDS)
        for name, expected in reference.expected.items():
            outcome = compare(expected, actual.get(name))
            score = scores[name]
            setattr(score, outcome, getattr(score, outcome) + 1)
            if outcome not in {"correct", "absent"}:
                score.errors.append(f"{number}: {outcome} (attendu {expected!r}, extrait {actual.get(name)!r})")
    return {name: s for name, s in scores.items() if s.total}
