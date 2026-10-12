"""Learned arbitration between candidate values of a field (form field, form text, decision clause, title, LLM).

Every source keeps its candidate (`Filing.candidates`). The chosen value maximises the probability that at least
one supporting source is right: 1 - prod(1 - reliability). Reliability per (field, source) starts from a prior that
reproduces the hand-written precedence (form first; decision first for commune and decision outcome; a bare
voltage is a weak substation) and is updated from human-verified values: (prior x W + correct) / (W + seen).
More reviews -> better choices, without code changes.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from radar.domain.fields import Value, field_values
from radar.domain.models import Candidate, Correction, Filing
from radar.evaluation.fields import compare, same
from radar.pipeline.corrections import VALIDATED, latest

ARBITRATED = ("developer", "siret", "commune", "power_mw", "substation", "filed_on", "review_decision")
DECISION_FIRST = frozenset({"commune", "review_decision"})
PRIOR_WEIGHT = 4.0
BASE_PRIOR: dict[str, float] = {
    "form:field": 0.9,
    "form:text": 0.8,
    "decision:article": 0.8,
    "decision:clause": 0.75,
    "decision:text": 0.7,
    "decision:title": 0.6,
    "llm": 0.65,
}
RULE_PRIOR = 0.7  # adopted rules (gold/rules.json): fill gaps; their real reliability is learned like any source
WEAK_VALUE_FACTOR = 0.5  # e.g. a substation given only as a voltage (« 400kV »)


def prior(field_name: str, source: str) -> float:
    base = RULE_PRIOR if source.startswith("rule:") else BASE_PRIOR.get(source, 0.6)
    if field_name in DECISION_FIRST:
        return min(0.95, base + 0.15) if source.startswith("decision") else base - 0.05
    return base


@dataclass
class Arbiter:
    stats: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)  # (field, source) -> (correct, seen)

    def reliability(self, field_name: str, source: str) -> float:
        correct, seen = self.stats.get((field_name, source), (0, 0))
        return (prior(field_name, source) * PRIOR_WEIGHT + correct) / (PRIOR_WEIGHT + seen)

    def score(self, field_name: str, candidate: Candidate) -> float:
        r = self.reliability(field_name, candidate.source)
        return r * WEAK_VALUE_FACTOR if candidate.weak else r

    def choose(self, field_name: str, candidates: list[Candidate]) -> tuple[Candidate, float, int] | None:
        """(best candidate, support score, number of agreeing sources)."""
        groups: list[list[Candidate]] = []
        for c in candidates:
            if c.value in (None, ""):
                continue
            for g in groups:
                if same(g[0].value, c.value):
                    g.append(c)
                    break
            else:
                groups.append([c])
        if not groups:
            return None

        def support(group: list[Candidate]) -> float:
            miss = 1.0
            for c in group:
                miss *= 1 - self.score(field_name, c)
            return 1 - miss

        best = max(groups, key=lambda g: (support(g), max(self.score(field_name, c) for c in g)))
        lead = max(best, key=lambda c: self.score(field_name, c))
        return lead, round(support(best), 4), len(best)

    def table(self) -> list[dict[str, object]]:
        keys = sorted({(f, s) for f in ARBITRATED for s in BASE_PRIOR} | set(self.stats))
        return [
            {
                "field": f,
                "source": s,
                "prior": round(prior(f, s), 3),
                "seen": self.stats.get((f, s), (0, 0))[1],
                "reliability": round(self.reliability(f, s), 3),
            }
            for f, s in keys
            if (f, s) in self.stats
        ]


def verified_values(filing: Filing, decisions: dict[str, Correction]) -> dict[str, Value]:
    """Human-verified values of a filing: corrected fields take the correction's value; a validation confirms the
    current values of the other fields."""
    out: dict[str, Value] = {}
    if VALIDATED in decisions:
        out.update(field_values(filing))
    out.update({name: c.value for name, c in decisions.items() if name != VALIDATED})
    return out


def learn(filings: Iterable[Filing], corrections: Iterable[Correction]) -> Arbiter:
    """Reliability of each source from human-verified values (validated filings and corrected fields)."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    for f in filings:
        decisions = latest(by_key.get(f.key, []))
        if not decisions:
            continue
        expected = verified_values(f, decisions)
        for name, options in f.candidates.items():
            if name not in expected:
                continue
            for candidate in options:
                ok = compare(expected[name], candidate.value) in {"correct", "absent"}
                correct, seen = stats.get((name, candidate.source), (0, 0))
                stats[(name, candidate.source)] = (correct + ok, seen + 1)
    return Arbiter(stats)


def comparable(value: object) -> Value:
    """Candidate value in the comparable form of domain.fields (dates ISO, enums by value)."""
    if value is None or isinstance(value, str | float):
        return value
    if isinstance(value, int):
        return float(value)
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(getattr(value, "value", value))
