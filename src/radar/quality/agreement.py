"""Double annotation: how reliable are the references themselves? Two reviewers on the same filing must agree;
where they do not, the guideline (or a reviewer) needs fixing, and accuracy figures above the agreement rate are
not meaningful.

Protocol: a stable `DOUBLE_RATE` share of reviewed filings returns to the queue for a second review, BLIND (values
typed without seeing the machine's or the first reviewer's), by another reviewer. Views compared:
- first reviewer: their explicit corrections, else the machine value they validated (validation snapshot);
- second reviewer: every value typed in blind mode (all explicit).
Agreement per field (with Wilson interval) and Cohen's kappa for the category (agreement beyond chance).
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from radar.domain.models import Correction, Filing
from radar.evaluation.fields import same
from radar.evaluation.stats import wilson
from radar.pipeline.corrections import CORRECTABLE, VALIDATED, approved_values, latest
from radar.quality.audit import stable_fraction

DOUBLE_RATE = 0.10


def sampled(number: str, rate: float = DOUBLE_RATE) -> bool:
    return stable_fraction(f"double:{number}") < rate


def _by_author(corrections: Iterable[Correction]) -> dict[str, list[Correction]]:
    out: dict[str, list[Correction]] = {}
    for c in sorted(corrections, key=lambda c: (c.created_at, c.id or 0)):
        out.setdefault(c.author, []).append(c)
    return out


def second_review_due(filing: Filing, corrections: Iterable[Correction]) -> bool:
    """A sampled review remains due until another author typed every sampled field blind."""
    return bool(second_review_fields(filing, corrections))


def second_review_fields(filing: Filing, corrections: Iterable[Correction]) -> list[str]:
    """Unfinished fields of the independent second annotation, not a whole-filing switch."""
    if not sampled(filing.number):
        return []
    authors = _by_author(corrections)
    first = next(((a, cs) for a, cs in authors.items() if any(c.field == VALIDATED for c in cs)), None)
    if first is None:
        return []
    author, decisions = first
    required = set(approved_values(latest(decisions))) & set(CORRECTABLE)
    # A blind global "validated" flag snapshots machine output; only explicitly typed
    # fields establish an independent second observation.
    covered = {
        c.field for other, cs in authors.items() if other != author for c in cs if c.blind and c.field != VALIDATED
    }
    return sorted(required - covered)


def _view(corrections: list[Correction]) -> dict[str, Any]:
    return dict(approved_values(latest(corrections)))


@dataclass
class Agreement:
    filings: int = 0
    by_field: dict[str, dict[str, Any]] = field(default_factory=dict)
    category_kappa: float | None = None
    disagreements: list[str] = field(default_factory=list)  # "number field: a | b" (dev split only, for guidelines)


def _agree(a: Any, b: Any) -> bool:
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    return same(a, b)


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two raters; None without pairs or when chance agreement is total."""
    if not pairs:
        return None
    observed = sum(a == b for a, b in pairs) / len(pairs)
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[k] * second[k] for k in first) / len(pairs) ** 2
    return None if expected == 1 else round((observed - expected) / (1 - expected), 3)


def agreement(filings: Iterable[Filing], corrections: Iterable[Correction], test_numbers: set[str]) -> Agreement:
    """Pairs = first reviewer vs a later BLIND reviewer. Test-split disagreements are counted, never listed."""
    by_filing: dict[str, list[Correction]] = {}
    for c in corrections:
        by_filing.setdefault(c.filing_key, []).append(c)
    out = Agreement()
    seen: Counter[str] = Counter()
    agreed: Counter[str] = Counter()
    categories: list[tuple[str, str]] = []
    for f in filings:
        authors = list(_by_author(by_filing.get(f.key, [])).items())
        if len(authors) < 2:
            continue
        (_, first), (_, second) = authors[0], authors[1]
        if not all(c.blind for c in second):
            continue
        a, b = _view(first), {c.field: c.value for c in second if c.field != VALIDATED}
        out.filings += 1
        for name in sorted(set(a) & set(b)):
            seen[name] += 1
            ok = _agree(a[name], b[name])
            agreed[name] += ok
            if not ok and f.number not in test_numbers:
                out.disagreements.append(f"{f.number} {name}: {a[name]!r} | {b[name]!r}")
            if name == "category" and a[name] and b[name]:
                categories.append((str(a[name]), str(b[name])))
    for name in sorted(seen):
        interval = wilson(agreed[name], seen[name])
        out.by_field[name] = {
            "pairs": seen[name],
            "agree": agreed[name],
            "rate": round(agreed[name] / seen[name], 3),
            "interval": [round(x, 3) for x in interval] if interval else None,
        }
    out.category_kappa = kappa(categories)
    return out
