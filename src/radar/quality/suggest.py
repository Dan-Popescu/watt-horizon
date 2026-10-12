"""Rule suggestions induced from failing regression cases, validated on every verified case.

For each field: locate the verified value in the documents of failing cases, take the words just before (left
anchor, 1-4 words) and the first word after (right anchor), build regexes `<anchor> (<value shape>) <anchor>` on
`fold_keep` text (lowercase, no accents, number punctuation kept), then replay each candidate on ALL cases with a verified value for that field:
fixes (failing cases it would solve), correct, wrong, spurious (a value where the reference says "absent").
Only candidates that fix something are returned, best first. They are proposals for a human or an agent to
turn into a rule (with a test), never applied automatically.
"""

import dataclasses
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from radar.evaluation.fields import compare
from radar.quality.cases import Case, check
from radar.quality.locate import MONTHS_FR, fold_keep, locate
from radar.quality.rulebook import LearnedRule
from radar.quality.values import parse

NUMBER = r"(\d[\d ]*(?:[.,]\d+)?)"
SHAPES: dict[str, str] = {
    "power_mw": NUMBER,
    "power_mw_min": NUMBER,
    "energy_mwh": NUMBER,
    "siret": r"(\d{3} ?\d{3} ?\d{3} ?\d{5})",
    "filed_on": rf"(\d{{1,2}}(?:er)? (?:{'|'.join(MONTHS_FR)}) \d{{4}}|\d{{2}}[ /.-]\d{{2}}[ /.-]\d{{4}})",
    "developer": r"([a-z0-9][a-z0-9' -]{1,60}?)",
    "commune": r"([a-z][a-z' -]{1,50}?)",
    "substation": r"([a-z][a-z0-9' -]{1,40}?)",
}
NAME_END = r"(?=\s*(?:[,;.(]|$))"
MIN_PRECISION = 0.9
STOPWORDS = frozenset(
    {"de", "du", "des", "d'", "la", "le", "les", "l'", "a", "au", "aux", "en", "et", "par", "sur", "pour", "un", "une"}
)


@dataclass
class Suggestion:
    field: str
    regex: str  # applies to fold_keep() text; group 1 = the value
    fixes: list[str] = dataclasses.field(default_factory=list)
    correct: int = 0
    wrong: int = 0
    spurious: int = 0

    @property
    def precision(self) -> float:
        tried = self.correct + self.wrong + self.spurious
        return self.correct / tried if tried else 0.0


def _folded(case: Case) -> str:
    return " ".join(fold_keep(d.text) for d in case.documents)


def _candidates(field_name: str, failing: list[Case]) -> Counter[str]:
    shape = SHAPES[field_name]
    found: Counter[str] = Counter()
    for case in failing:
        texts = {d.id: d.text for d in case.documents}
        for span in locate(field_name, case.expected[field_name], texts, limit=3):
            left = fold_keep(span.left).split()[-4:]
            right = fold_keep(span.right).split()[:1]
            for n in range(1, len(left) + 1):
                if all(w in STOPWORDS for w in left[-n:]):
                    continue  # « de », « la »: matches anything at scale, however good it looks on few cases
                anchor = r"\b" + r"\s+".join(re.escape(w) for w in left[-n:]) + r"\s+"
                if field_name in {"developer", "commune", "substation"}:
                    found[anchor + shape + NAME_END] += 1
                if right:
                    found[anchor + shape + r"\s*" + re.escape(right[0]) + r"\b"] += 1
                elif field_name not in {"developer", "commune", "substation"}:
                    found[anchor + shape] += 1
    return found


def suggest(cases: list[Case], field_name: str, limit: int = 5, rules: Sequence[LearnedRule] = ()) -> list[Suggestion]:
    """Validated candidates for one field, from cases whose replay fails on it."""
    if field_name not in SHAPES:
        return []
    relevant = [c for c in cases if field_name in c.expected]
    failing = [c for c in relevant if not check(c, rules)[field_name] and c.expected[field_name] is not None]
    results = []
    for regex in _candidates(field_name, failing):
        compiled = re.compile(regex)
        s = Suggestion(field_name, regex)
        for case in relevant:
            m = compiled.search(_folded(case))
            if not m:
                continue
            outcome = compare(case.expected[field_name], parse(field_name, m[1]))
            if outcome == "correct":
                s.correct += 1
                if case in failing:
                    s.fixes.append(case.number)
            elif outcome == "spurious":
                s.spurious += 1
            elif outcome == "wrong":
                s.wrong += 1
        if s.fixes and s.precision >= MIN_PRECISION:
            results.append(s)
    # Same fixes and precision: prefer the shortest meaningful anchor (generalises to new wordings).
    results.sort(key=lambda s: (-len(s.fixes), -s.precision, -s.correct, s.regex.count(r"\s+"), len(s.regex)))
    return results[:limit]
