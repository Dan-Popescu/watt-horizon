"""Human review of links: the project level of the quality loop (a wrong merge hides a project, a missed merge
shows a duplicate). Decisions (`LinkDecision`) override the automatic grouping at every build; this module
annotates links with them, orders the pairs worth a look, and measures the linker and the grouping against them.
"""

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from radar.domain.enums import TARGET_CATEGORIES
from radar.domain.models import Filing, Link, LinkDecision
from radar.evaluation.stats import wilson
from radar.linking.resolve import group_filings

HUMAN_REASON = "lien posé par un relecteur"
CONFIRMED = {"same_project", "related"}


def apply_link_decisions(links: list[Link], decisions: Mapping[tuple[str, str], LinkDecision]) -> list[Link]:
    """Links annotated with the latest verdict; a « same_project » pair the linker missed is added."""
    return list(iter_apply_link_decisions(links, decisions))


def iter_apply_link_decisions(
    links: Iterable[Link],
    decisions: Mapping[tuple[str, str], LinkDecision],
    *,
    include_missing_constraints: bool = False,
) -> Iterator[Link]:
    """Remember only human pairs, independently of the potentially quadratic machine graph."""
    missing = {
        pair
        for pair, decision in decisions.items()
        if decision.verdict == "same_project"
        or (include_missing_constraints and decision.verdict in {"related", "unrelated"})
    }
    for link in links:
        pair = (link.a, link.b)
        missing.discard(pair)
        decision = decisions.get(pair)
        verdict = None if decision and decision.verdict == "undecided" else decision.verdict if decision else None
        yield link.model_copy(update={"verdict": verdict}) if decision else link
    for a, b in sorted(missing):
        yield Link(a=a, b=b, reasons=(HUMAN_REASON,), verdict=decisions[(a, b)].verdict)


def machine_groups(filings: Iterable[Filing]) -> dict[str, int]:
    """Filing key -> group number of the automatic grouping (no human decision): what the machine alone does."""
    targets = [f for f in filings if f.classification.category in TARGET_CATEGORIES]
    return {f.key: i for i, group in enumerate(group_filings(targets)) for f in group}


def reason_kind(reason: str) -> str:
    return reason.split(" :")[0].split(" (")[0]


@dataclass
class LinkPair:
    a: str
    b: str
    reasons: tuple[str, ...]
    verdict: str | None
    merged_by_machine: bool  # both filings in the same automatic group
    priority: float


def iter_queue(filings: list[Filing], links: Iterable[Link]) -> Iterator[LinkPair]:
    """Links touching a target filing, undecided and most consequential first:
    merged by the machine (a wrong merge hides a project) > same developer + commune but apart (missed merge) >
    other filing pairs > filing-signal pairs."""
    targets = {f.key for f in filings if f.classification.category in TARGET_CATEGORIES}
    filing_keys = {f.key for f in filings}
    groups = machine_groups(filings)
    for lk in links:
        if lk.a not in targets and lk.b not in targets:
            continue
        both_filings = lk.a in filing_keys and lk.b in filing_keys
        merged = both_filings and groups.get(lk.a) is not None and groups.get(lk.a) == groups.get(lk.b)
        kinds = {reason_kind(r) for r in lk.reasons}
        priority = 3.0 if merged else 2.0 if {"même commune", "même nom de porteur"} <= kinds else 1.0
        priority = (priority if both_filings else 0.5) + 0.1 * len(kinds)
        yield LinkPair(lk.a, lk.b, lk.reasons, lk.verdict, merged, 0.0 if lk.verdict else priority)


def queue(filings: list[Filing], links: Iterable[Link]) -> list[LinkPair]:
    """Compatibility sorted result; iter_queue lets views avoid a duplicate graph collection."""
    return sorted(iter_queue(filings, links), key=lambda p: (-p.priority, p.a, p.b))


@dataclass
class LinkReport:
    decided: int = 0
    by_reason: dict[str, dict[str, Any]] = field(default_factory=dict)  # reason kind -> confirmed share
    grouping: dict[str, Any] = field(default_factory=dict)  # pairwise precision / recall of automatic merges


def report(
    filings: list[Filing], links: Iterable[Link], decisions: Mapping[tuple[str, str], LinkDecision]
) -> LinkReport:
    """Linker precision per reason (a confirmed link is « same_project » or « related ») and grouping quality on
    filing pairs a human decided (merged by the machine vs « same_project »)."""
    out = LinkReport(decided=len(decisions))
    seen: Counter[str] = Counter()
    good: Counter[str] = Counter()
    for lk in links:
        d = decisions.get((lk.a, lk.b))
        if d is None or lk.reasons == (HUMAN_REASON,):
            continue
        for kind in {reason_kind(r) for r in lk.reasons}:
            seen[kind] += 1
            good[kind] += d.verdict in CONFIRMED
    for kind in sorted(seen):
        interval = wilson(good[kind], seen[kind])
        out.by_reason[kind] = {
            "decided": seen[kind],
            "confirmed": good[kind],
            "interval": [round(x, 3) for x in interval] if interval else None,
        }
    groups = machine_groups(filings)
    tp = fp = fn = 0
    for (a, b), d in decisions.items():
        if a not in groups or b not in groups:
            continue
        merged, same = groups[a] == groups[b], d.verdict == "same_project"
        tp += merged and same
        fp += merged and not same
        fn += same and not merged
    out.grouping = {
        "pairs": tp + fp + fn,
        "wrong_merges": fp,
        "missed_merges": fn,
        "precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "recall": round(tp / (tp + fn), 3) if tp + fn else None,
    }
    return out
