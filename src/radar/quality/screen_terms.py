"""Screen-term suggestions: words that would have kept the targets the layer-1 screen missed, with their cost.

Two kinds of miss, both found automatically once data flows:
- title miss: a target filing whose documents were fetched only as an audit (the title screen rejected them);
  candidates = 1-3 word phrases of its titles; cost = rejected listing titles they would also admit (extra fetches);
- text miss: a target filing the full-text screen dropped; candidates = its vocabulary words; cost = non-target
  filings containing them (extra classification work).
A suggestion is only a proposal: `radar screen-terms adopt --term "..."` puts it in gold/screen_terms.json.
"""

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from radar.classification.bayes import STOPWORDS
from radar.domain.enums import TARGET_CATEGORIES, Category
from radar.domain.models import DocumentRecord, Filing
from radar.domain.text import fold
from radar.evaluation.stats import partition_of

MAX_ADMIT_SHARE = 0.05  # a term admitting more than 5 % of the negatives is too broad
_STOP = {fold(w) for w in STOPWORDS} | {"de", "du", "la", "le", "et", "en", "au", "un", "commune", "projet"}


@dataclass
class TermSuggestion:
    term: str
    scope: str  # title | text
    rescues: list[str]  # filing numbers it would have kept
    admits: int  # negatives it would also keep
    negatives: int
    examples: list[str] = field(default_factory=list)

    @property
    def admit_share(self) -> float:
        return self.admits / self.negatives if self.negatives else 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"admit_share": round(self.admit_share, 3)}


def _phrases(title: str) -> set[str]:
    words = fold(title).split()
    out: set[str] = set()
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            gram = words[i : i + n]
            if gram[0] in _STOP or gram[-1] in _STOP or any(w.isdigit() for w in gram):
                continue
            if n == 1 and len(gram[0]) < 5:
                continue
            out.add(" ".join(gram))
    return out


def _contains(text: str, term: str) -> bool:
    return f" {term} " in f" {fold(text)} "


def suggest(
    filings: list[Filing],
    documents: dict[str, list[DocumentRecord]],
    rejected_titles: list[tuple[str, str]],
    current: Any = None,
) -> list[TermSuggestion]:
    """`documents`: filing key -> documents; `rejected_titles`: (url, title) of listing items the screen rejected.
    `current`: adopted terms pattern; phrases it already matches are skipped."""
    excluded_urls = {d.url for f in filings if partition_of(f) != "dev" for d in documents.get(f.key, [])}
    filings = [f for f in filings if partition_of(f) == "dev"]
    rejected_titles = [(url, title) for url, title in rejected_titles if url not in excluded_urls]
    targets = [f for f in filings if f.classification.category in TARGET_CATEGORIES]
    out: list[TermSuggestion] = []

    title_misses = {f.number: [d for d in documents.get(f.key, []) if d.audit] for f in targets}
    title_misses = {n: docs for n, docs in title_misses.items() if docs}
    target_urls = {d.url for f in targets for d in documents.get(f.key, [])}
    negatives = [t for url, t in rejected_titles if url not in target_urls]
    by_phrase: dict[str, list[str]] = {}
    for number, docs in title_misses.items():
        for phrase in set().union(*(_phrases(d.title) for d in docs)):
            if current is None or not current.search(phrase):
                by_phrase.setdefault(phrase, []).append(number)
    for phrase, numbers in by_phrase.items():
        admits = sum(_contains(t, phrase) for t in negatives)
        examples = [d.title[:160] for n in numbers[:2] for d in title_misses[n][:1]]
        out.append(TermSuggestion(phrase, "title", sorted(numbers), admits, len(negatives), examples))

    dropped = [
        f
        for f in targets
        if f.machine is not None and f.machine.classification.method == "rules"
        and f.machine.classification.category is Category.OTHER
    ]  # fmt: skip
    others = [f for f in filings if f.classification.category not in TARGET_CATEGORIES and f.vocabulary]
    if dropped:
        seen: Counter[str] = Counter(w for f in others for w in set(f.vocabulary))
        rescued: dict[str, list[str]] = {}
        for f in dropped:
            for w in f.vocabulary:
                if len(w) >= 5 and w not in _STOP and (current is None or not current.search(w)):
                    rescued.setdefault(w, []).append(f.number)
        for w, numbers in rescued.items():
            out.append(TermSuggestion(w, "text", sorted(numbers), seen[w], len(others)))

    kept = [s for s in out if s.admit_share <= MAX_ADMIT_SHARE]
    return sorted(kept, key=lambda s: (-len(s.rescues), s.admits, -len(s.term.split()), s.term))
