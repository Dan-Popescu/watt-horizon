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
from radar.pipeline.filings import Services, classification_text, read_documents
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

    @property
    def recall(self) -> float | None:
        return self.screened_in / self.gold_targets if self.gold_targets else None

    @property
    def precision(self) -> float | None:
        """Share of kept gold filings that are targets: low is fine (layer 2 filters), it drives LLM cost."""
        return self.screened_in / self.kept_gold if self.kept_gold else None


def screening_report(filings: list[Filing], gold: dict[str, GoldLabel]) -> ScreeningReport:
    report = ScreeningReport(by_method=dict(Counter(machine_classification(f).method for f in filings)))
    by_number = {f.number: f for f in filings}
    for number, label in gold.items():
        filing = by_number.get(number)
        kept = filing is not None and not dropped_by_screen(filing)
        report.kept_gold += kept
        if label.category in TARGET_CATEGORIES:
            report.gold_targets += 1
            if kept:
                report.screened_in += 1
            else:
                report.missed.append(number)
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

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.evaluated if self.evaluated else None


def evaluate_classifier(
    classifier: Classifier, db: Database, services: Services, gold: dict[str, GoldLabel]
) -> ClassifierReport:
    report = ClassifierReport(classifier=classifier.name)
    for filing in db.filings():
        label = gold.get(filing.number)
        if label is None or dropped_by_screen(filing):
            continue
        documents = db.documents(filing.key)
        contents = read_documents(documents, services)
        forms = [c.fields for d, c in zip(documents, contents, strict=True) if d.role is DocumentRole.FORM]
        privacy = PersonScrubber.for_filing(forms, [filing.developer, filing.legal_name])
        texts = [privacy.scrub(c.text) or "" for c in contents]
        if not screen(filing.title, filing.project_description, *texts).keep:
            continue
        item = ClassificationInput(
            filing.key, filing.number, filing.title, classification_text(filing.project_description, texts)
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
        else:
            report.errors.append(f"{filing.number}: attendu {label.category.value}, prédit {predicted}")
        report.quotes_verified += bool(result and result.quote_verified)
    return report
