"""The `Classifier` port, and a chain that tries classifiers in order."""

from dataclasses import dataclass
from typing import Protocol

from radar.domain.models import Classification


@dataclass(frozen=True)
class ClassificationInput:
    filing_key: str
    number: str
    title: str
    text: str  # fields + document texts, already whitespace-squashed


class Classifier(Protocol):
    name: str

    def classify(self, item: ClassificationInput) -> Classification | None:
        """Return None when this classifier cannot decide (unavailable, refusal, unknown item)."""
        ...


class ChainClassifier:
    def __init__(self, *classifiers: Classifier) -> None:
        self.classifiers = classifiers
        self.name = "+".join(c.name for c in classifiers) or "none"

    def classify(self, item: ClassificationInput) -> Classification | None:
        for classifier in self.classifiers:
            if result := classifier.classify(item):
                return result
        return None
