"""Classification quality: layer-1 screening (recall must stay 1.0) and model agreement with the gold labels.

- `screening_report`: from stored filings (offline, every run).
- `evaluate_classifier`: re-runs one classifier alone (e.g. the LLM, without the gold fallback) on every
  gold-labelled filing that passes layer 1; gives accuracy, a confusion matrix and the verified-quote rate.
"""

from collections import Counter
from dataclasses import dataclass, field

from radar.classification.base import ClassificationInput, Classifier
from radar.classification.gold import GoldLabel
from radar.classification.rules import screen
from radar.domain.enums import TARGET_CATEGORIES, Category, DocumentRole
from radar.domain.models import Classification, Filing
from radar.domain.privacy import PersonScrubber
from radar.domain.references import canonical_reference_map
from radar.evaluation.stats import partition_of
from radar.pipeline.filings import Services, classification_text, decision_first, read_documents
from radar.store.db import Database


def machine_classification(filing: Filing) -> Classification:
    return filing.machine.classification if filing.machine else filing.classification


def dropped_by_screen(filing: Filing) -> bool:
    machine = machine_classification(filing)
    return machine.method == "rules" and machine.category is Category.OTHER


@dataclass
class ScreeningReport:
    gold_targets: int = 0
    screened_in: int = 0  # gold targets kept by layer 1
    kept_gold: int = 0  # gold-labelled filings kept by layer 1 (precision denominator)
    missed: list[str] = field(default_factory=list)
    by_method: dict[str, int] = field(default_factory=dict)  # how every filing got its category
    groups: set[str] = field(default_factory=set)
    target_groups: dict[str, bool] = field(default_factory=dict)

    @property
    def independent_n(self) -> int:
        return len(self.groups)

    @property
    def independent_target_n(self) -> int:
        return len(self.target_groups)

    @property
    def independent_recall(self) -> float | None:
        return sum(self.target_groups.values()) / len(self.target_groups) if self.target_groups else None

    @property
    def recall(self) -> float | None:
        return self.screened_in / self.gold_targets if self.gold_targets else None

    @property
    def precision(self) -> float | None:
        """Share of kept gold filings that are targets: low is fine (layer 2 filters), it drives LLM cost."""
        return self.screened_in / self.kept_gold if self.kept_gold else None


def screening_report(filings: list[Filing], gold: dict[str, GoldLabel]) -> ScreeningReport:
    report = ScreeningReport(by_method=dict(Counter(machine_classification(f).method for f in filings)))
    by_key = {f.key: f for f in filings}
    for key, label in canonical_reference_map(filings, gold).items():
        filing = by_key.get(key)
        kept = filing is not None and not dropped_by_screen(filing)
        report.kept_gold += kept
        group = filing.strata.get("evaluation_group", filing.key) if filing else None
        if group:
            report.groups.add(group)
        if label.category in TARGET_CATEGORIES:
            report.gold_targets += 1
            if group:
                report.target_groups[group] = report.target_groups.get(group, False) or kept
            if kept:
                report.screened_in += 1
            else:
                report.missed.append(key)
    return report


@dataclass
class ClassifierReport:
    classifier: str
    evaluated: int = 0
    correct: int = 0
    no_answer: int = 0
    quotes_verified: int = 0
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)  # gold -> predicted -> count
    errors: list[str] = field(default_factory=list)
    test_independent_n: int = 0
    test_independent_correct: int = 0

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.evaluated if self.evaluated else None

    @property
    def test_independent_accuracy(self) -> float | None:
        return self.test_independent_correct / self.test_independent_n if self.test_independent_n else None


def evaluate_classifier(
    classifier: Classifier, db: Database, services: Services, gold: dict[str, GoldLabel]
) -> ClassifierReport:
    if "gold" in classifier.name.split("+"):
        raise ValueError("A reference classifier cannot be evaluated against its own gold labels")
    report = ClassifierReport(classifier=classifier.name)
    filings = db.filings()
    references = canonical_reference_map(filings, gold)
    test_groups: dict[str, bool] = {}
    for filing in filings:
        label = references.get(filing.key)
        if label is None or not label.verified or dropped_by_screen(filing):
            continue
        documents = db.documents(filing.key)
        contents = read_documents(documents, services)
        forms = [c.fields for d, c in zip(documents, contents, strict=True) if d.role is DocumentRole.FORM]
        privacy = PersonScrubber.for_filing(forms, [filing.developer, filing.legal_name])
        texts = [privacy.scrub(c.text) or "" for c in contents]
        if not screen(filing.title, filing.project_description, *texts, extra=services.screen_terms).keep:
            continue
        item = ClassificationInput(
            filing.key,
            filing.number,
            filing.title,
            classification_text(filing.project_description, decision_first(documents, texts)),
        )
        result = classifier.classify(item)
        report.evaluated += 1
        predicted = result.category.value if result else "no_answer"
        row = report.confusion.setdefault(label.category.value, {})
        row[predicted] = row.get(predicted, 0) + 1
        if result is None:
            report.no_answer += 1
        elif result.category is label.category:
            report.correct += 1
        elif partition_of(filing) == "dev":
            report.errors.append(f"{filing.number}: attendu {label.category.value}, prédit {predicted}")
        report.quotes_verified += bool(result and result.quote_verified)
        if partition_of(filing) == "test":
            group = filing.strata.get("evaluation_group", filing.key)
            test_groups[group] = test_groups.get(group, True) and bool(result and result.category is label.category)
    report.test_independent_n = len(test_groups)
    report.test_independent_correct = sum(test_groups.values())
    return report
