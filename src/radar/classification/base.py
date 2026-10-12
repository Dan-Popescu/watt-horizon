"""The `Classifier` port, and a chain that tries classifiers in order."""

from dataclasses import dataclass
from typing import Protocol

from radar.domain.models import Classification
from radar.store.cache import KeyValueCache, cache_key


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


class CachedClassifier:
    """Memoises a paid classifier: same (classifier name incl. model + prompt version, title, text) -> same answer."""

    NAMESPACE = "classification"

    def __init__(self, inner: Classifier, cache: KeyValueCache) -> None:
        self.inner, self.cache = inner, cache
        self.name = inner.name

    def classify(self, item: ClassificationInput) -> Classification | None:
        # A paid classifier keys on its exact request (`key`): what is not sent cannot change the answer.
        key = self.inner.key(item) if hasattr(self.inner, "key") else cache_key(self.name, item.title, item.text)
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            return Classification.model_validate(hit["result"]) if hit["result"] else None
        result = self.inner.classify(item)
        self.cache.put(self.NAMESPACE, key, {"result": result.model_dump(mode="json") if result else None})
        return result
