"""Human-reviewed labels ("gold set"): used to evaluate classifiers and as a fallback when no LLM is configured.

File format (JSON Lines), one filing per line:
  {"number": "2025-8646", "category": "bess", "note": "...", "annotator": "...", "verified": true}
`verified` is true only once a human confirmed the label (review page or by hand); drafts still serve as fallback.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.classification.base import ClassificationInput
from radar.domain.enums import Category
from radar.domain.models import Classification


@dataclass(frozen=True)
class GoldLabel:
    number: str
    category: Category
    note: str = ""
    annotator: str = ""
    verified: bool = False
    filing_key: str | None = None


def load_gold(path: Path) -> dict[str, GoldLabel]:
    if not path.exists():
        return {}
    labels: dict[str, GoldLabel] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            label = GoldLabel(
                row["number"],
                Category(row["category"]),
                row.get("note", ""),
                row.get("annotator", ""),
                bool(row.get("verified", False)),
                row.get("filing_key"),
            )
            identity = label.filing_key or label.number
            if identity in labels and labels[identity] != label:
                raise ValueError(f"Conflicting classification references for {identity!r}")
            labels[identity] = label
    return labels


class GoldClassifier:
    name = "gold"

    def __init__(self, labels: dict[str, GoldLabel], *, known_keys: Iterable[str] = ()) -> None:
        self.labels: dict[str, GoldLabel] = {}
        for identity, label in labels.items():
            key = label.filing_key or identity
            if key in self.labels and self.labels[key] != label:
                raise ValueError(f"Conflicting classification references for {key!r}")
            self.labels[key] = label
        self.by_number: dict[str, set[str]] = {}
        for key in known_keys:
            self.by_number.setdefault(key.rsplit(":", 1)[-1], set()).add(key)
        policy = json.dumps(
            {key: asdict(label) for key, label in self.labels.items()},
            sort_keys=True,
            default=str,
        )
        self.name = "gold:" + hashlib.sha256(policy.encode()).hexdigest()[:16]

    def classify(self, item: ClassificationInput) -> Classification | None:
        label = self.labels.get(item.filing_key)
        if label is None and item.number in self.labels:
            candidates = self.by_number.get(item.number, set())
            if len(candidates) > 1:
                raise ValueError(f"Ambiguous legacy classification number {item.number!r}; qualify its filing_key")
            if candidates == {item.filing_key}:
                label = self.labels[item.number]
        if label is None or label.number != item.number:
            return None
        return Classification(category=label.category, method="gold", confidence="reviewed", quote=label.note)
