"""A local second opinion on the category: naive Bayes over each filing's vocabulary, re-learned at every run from
trusted labels only (verified gold labels, human corrections). No API, no cost, fully explainable (its clue words).

It never decides. When it is reliable (enough labels, good leave-one-out accuracy) and confidently disagrees with
the category, the filing is flagged for review: a wrong label, a classifier error, or a target the screen dropped.

Vocabulary = lowercase words only (plus a few domain acronyms), from GDPR-scrubbed text: names are capitalised, so
they are left out by construction; it is stored on the filing so learning needs no document re-read.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

from radar.domain.enums import Category
from radar.domain.models import Filing
from radar.domain.references import canonical_reference_map
from radar.domain.text import fold
from radar.evaluation.stats import partition_of

MAX_CHARS = 40_000
MIN_LABELS = 30  # below this, no opinion
MIN_ACCURACY = 0.8  # leave-one-out accuracy required before the opinion raises flags
CONFIDENT = 0.9  # posterior needed to flag a disagreement
_WORD = re.compile(r"(?<![\w-])[a-zàâçéèêëîïôûùüÿœ]{3,}(?![\w-])")
ACRONYMS = frozenset({"bess", "mwh", "mwc", "kv", "rte", "enedis", "htb", "hta", "icpe", "pue", "ups"})
_ACRONYM = re.compile(r"\b(" + "|".join(ACRONYMS) + r")\b", re.IGNORECASE)
STOPWORDS = frozenset({
    "les", "des", "une", "pour", "par", "sur", "dans", "avec", "aux", "est", "sont", "qui", "que", "pas", "plus",
    "son", "ses", "leur", "cette", "ces", "ont", "été", "être", "fait", "ainsi", "entre", "sous", "vers", "lors",
    "dont", "comme", "tout", "tous", "toute", "toutes", "elle", "elles", "ils", "nom", "masqué", "courriel",
    "téléphone",
})  # fmt: skip


def vocabulary(text: str) -> tuple[str, ...]:
    """Sorted distinct folded words of `text`: lowercase words (names excluded) and domain acronyms."""
    text = text[:MAX_CHARS]
    words = {fold(w) for w in _WORD.findall(text) if w not in STOPWORDS}
    words |= {m.lower() for m in _ACRONYM.findall(text)}
    return tuple(sorted(words - {fold(s) for s in STOPWORDS}))


@dataclass(frozen=True)
class Opinion:
    category: Category
    probability: float
    clues: tuple[str, ...]  # words that weigh most for this category

    def describe(self) -> str:
        return f"{self.category.value} (p={self.probability:.2f} ; mots : {', '.join(self.clues)})"


@dataclass(frozen=True)
class BayesModel:
    docs: dict[Category, int]  # labelled filings per category
    counts: dict[Category, Counter[str]]  # filings of the category containing the word
    vocabulary: frozenset[str]  # words seen in at least 2 labelled filings
    absent: dict[Category, float]  # log-likelihood of a filing containing no vocabulary word (Bernoulli base)
    loo_correct: int = 0
    loo_total: int = 0

    @property
    def loo_accuracy(self) -> float | None:
        return self.loo_correct / self.loo_total if self.loo_total else None

    @property
    def reliable(self) -> bool:
        return self.loo_total >= MIN_LABELS and (self.loo_accuracy or 0) >= MIN_ACCURACY

    def _scores(self, present: set[str], leave_out: tuple[Category, set[str]] | None = None) -> dict[Category, float]:
        """Log-posterior per category; `leave_out`: score as if that labelled filing were not in the counts."""
        total = sum(self.docs.values()) - (leave_out is not None)
        scores: dict[Category, float] = {}
        for cat, n in self.docs.items():
            minus = leave_out[1] if leave_out is not None and leave_out[0] is cat else set()
            n -= leave_out is not None and leave_out[0] is cat
            if n <= 0:
                continue
            counts = self.counts[cat]
            base = self.absent[cat] if not minus else _absent(counts, self.vocabulary, n, minus)
            score = math.log(n / total) + base
            for w in present:  # Bernoulli: switch the word's term from « absent » to « present »
                p = (counts[w] - (w in minus) + 1) / (n + 2)
                score += math.log(p) - math.log(1 - p)
            scores[cat] = score
        return scores

    def predict(self, words: tuple[str, ...]) -> Opinion | None:
        present = set(words) & self.vocabulary
        scores = self._scores(present)
        if not scores:
            return None
        best = max(scores, key=lambda c: scores[c])
        top = max(scores.values())
        probability = 1 / sum(math.exp(s - top) for s in scores.values())
        others = [c for c in self.docs if c is not best]

        def lift(w: str) -> float:  # how much more often the word appears in this category than in the others
            mine = (self.counts[best][w] + 1) / (self.docs[best] + 2)
            rest = (sum(self.counts[c][w] for c in others) + 1) / (sum(self.docs[c] for c in others) + 2)
            return mine / rest

        clues = tuple(sorted(present, key=lambda w: (-lift(w), w))[:5])
        return Opinion(best, probability, clues)


def _absent(counts: Counter[str], vocabulary: frozenset[str], n: int, minus: set[str] | frozenset[str]) -> float:
    return sum(math.log(1 - (counts[w] - (w in minus) + 1) / (n + 2)) for w in vocabulary)


def train(examples: list[tuple[tuple[str, ...], Category]]) -> BayesModel | None:
    """None without at least two categories. Leave-one-out accuracy is computed exactly (counts minus the item)."""
    docs = Counter(cat for _, cat in examples)
    if len(docs) < 2:
        return None
    counts: dict[Category, Counter[str]] = {cat: Counter() for cat in docs}
    seen: Counter[str] = Counter()
    for words, cat in examples:
        counts[cat].update(set(words))
        seen.update(set(words))
    vocab = frozenset(w for w, n in seen.items() if n >= 2)
    absent = {cat: _absent(counts[cat], vocab, docs[cat], frozenset()) for cat in docs}
    model = BayesModel(dict(docs), counts, vocab, absent)
    correct = 0
    for words, cat in examples:
        scores = model._scores(set(words) & vocab, leave_out=(cat, set(words)))
        correct += bool(scores) and max(scores, key=lambda c: scores[c]) is cat
    return BayesModel(model.docs, counts, vocab, absent, correct, len(examples))


def trusted_examples(filings: list[Filing], labels: dict[str, Category]) -> list[tuple[tuple[str, ...], Category]]:
    """(vocabulary, category) for filings whose category a human set or verified: the second opinion's training set."""
    out = []
    resolved = canonical_reference_map(filings, labels)
    for f in filings:
        if partition_of(f) != "dev":
            continue
        human = f.classification.category if f.classification.method == "human" else None
        label = human or resolved.get(f.key)
        if label is not None and f.vocabulary:
            out.append((f.vocabulary, label))
    return out


def summary(model: BayesModel | None) -> dict[str, object]:
    """For the evaluation report: is the second opinion active, and how good is it (leave-one-out)?"""
    if model is None:
        return {"active": False, "why": "fewer than two categories among trusted labels"}
    return {
        "active": model.reliable,
        "labels": model.loo_total,
        "loo_accuracy": round(model.loo_accuracy, 3) if model.loo_accuracy is not None else None,
        "vocabulary": len(model.vocabulary),
        "needs": f">= {MIN_LABELS} trusted labels and leave-one-out accuracy >= {MIN_ACCURACY}",
    }
