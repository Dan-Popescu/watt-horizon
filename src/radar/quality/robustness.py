"""Metamorphic robustness: perturb document texts the way real PDFs differ, check the extraction does not move.

No reference needed: the unperturbed extraction is the reference (self-consistency). A value that changes under
a meaning-preserving perturbation reveals a brittle rule before real documents do. Run on the whole stored
corpus (`radar robustness`) or on regression cases; `radar eval` reports the stable share.
"""

import random
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from radar.domain.enums import DocumentRole
from radar.domain.fields import Value
from radar.evaluation.fields import compare
from radar.pipeline.filings import ARBITRATED_ATTRS, extract_facts
from radar.reading.pdf import PdfContent


def _line_breaks(text: str, rng: random.Random) -> str:
    return re.sub(r" ", lambda m: "\n" if rng.random() < 0.15 else " ", text)


def _double_spaces(text: str, rng: random.Random) -> str:
    return re.sub(r" ", lambda m: " " * rng.choice((1, 2, 3)), text)


def _apostrophes(text: str, rng: random.Random) -> str:
    return text.translate(str.maketrans("'’", "’'"))


def _no_accents(text: str, rng: random.Random) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _uppercase(text: str, rng: random.Random) -> str:
    return text.upper()


def _nbsp_numbers(text: str, rng: random.Random) -> str:
    """Spaces next to digits become non-breaking or thin spaces (« 47 MW », « 1 292 »)."""
    return re.sub(r"(?<=\d) | (?=\d)", lambda m: rng.choice((" ", " ")), text)


def _hyphenation(text: str, rng: random.Random) -> str:
    """Line-end hyphenation inside long words (« installa-\\ntion »), frequent in PDF text layers."""

    def cut(m: re.Match[str]) -> str:
        word = m[0]
        if rng.random() < 0.1:
            i = len(word) // 2
            return f"{word[:i]}-\n{word[i:]}"
        return word

    return re.sub(r"[A-Za-zÀ-ÿ]{9,}", cut, text)


PERTURBATIONS: dict[str, Callable[[str, random.Random], str]] = {
    "line_breaks": _line_breaks,
    "double_spaces": _double_spaces,
    "apostrophes": _apostrophes,
    "no_accents": _no_accents,
    "uppercase": _uppercase,
    "nbsp_numbers": _nbsp_numbers,
    "hyphenation": _hyphenation,
}
FIELDS = (*ARBITRATED_ATTRS, "energy_mwh")


@dataclass(frozen=True)
class Document:
    id: str
    role: DocumentRole
    title: str
    text: str
    fields: dict[str, str]


@dataclass
class Fragility:
    number: str
    field: str
    perturbation: str
    before: Value
    after: Value


@dataclass
class RobustnessReport:
    checks: Counter[str] = field(default_factory=Counter)  # perturbation -> values compared
    fragile: list[Fragility] = field(default_factory=list)

    @property
    def stable_share(self) -> float | None:
        total = sum(self.checks.values())
        return round(1 - len(self.fragile) / total, 4) if total else None

    def summary(self) -> dict[str, object]:
        broken = Counter(f.perturbation for f in self.fragile)
        return {
            "stable_share": self.stable_share,
            "by_perturbation": {p: f"{n - broken[p]}/{n}" for p, n in self.checks.items()},
            "by_field": dict(Counter(f.field for f in self.fragile)),
        }


def _values(documents: list[Document], transform: Callable[[str], str]) -> dict[str, Value]:
    contents = [
        PdfContent(pages=1, fields=d.fields, page_texts=(transform(d.text),), scanned_pages=()) for d in documents
    ]
    facts = extract_facts([(d.id, d.role, d.title) for d in documents], contents).facts
    out: dict[str, Value] = {}
    for name in FIELDS:
        attr = "mwh" if name == "energy_mwh" else ARBITRATED_ATTRS[name][0]
        value = getattr(facts, attr)
        out[name] = value.isoformat() if hasattr(value, "isoformat") else getattr(value, "value", value)
    return out


def check_robustness(items: list[tuple[str, list[Document]]], seed: int = 0) -> RobustnessReport:
    report = RobustnessReport()
    for number, documents in items:
        reference = _values(documents, lambda t: t)
        for name, perturb in PERTURBATIONS.items():
            rng = random.Random(f"{seed}:{number}:{name}")
            got = _values(documents, lambda t, p=perturb, r=rng: p(t, r))  # type: ignore[misc]
            for f in FIELDS:
                if reference[f] in (None, "") and got[f] in (None, ""):
                    continue
                report.checks[name] += 1
                if compare(reference[f], got[f]) not in {"correct", "absent"}:
                    report.fragile.append(Fragility(number, f, name, reference[f], got[f]))
    return report


def render(report: RobustnessReport) -> str:
    lines = [
        "# Robustness: values that move under meaning-preserving perturbations",
        "",
        f"Summary: `{report.summary()}`",
        "",
        "Each line is a brittle rule: make the regex tolerant (\\s+, [’'], optional accents, \\u00a0), add a test.",
        "",
        "| case | field | perturbation | clean text gives | perturbed gives |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [f"| {f.number} | {f.field} | {f.perturbation} | `{f.before}` | `{f.after}` |" for f in report.fragile]
    return "\n".join(lines) + "\n"
