"""Human-reviewed labels ("gold set"): used to evaluate classifiers and as a fallback when no LLM is configured.

File format (JSON Lines), one filing per line:
  {"number": "2025-8646", "category": "bess", "note": "...", "annotator": "..."}
"""

import json
from dataclasses import dataclass
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


def load_gold(path: Path) -> dict[str, GoldLabel]:
    if not path.exists():
        return {}
    labels: dict[str, GoldLabel] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            label = GoldLabel(row["number"], Category(row["category"]), row.get("note", ""), row.get("annotator", ""))
            labels[label.number] = label
    return labels


class GoldClassifier:
    name = "gold"

    def __init__(self, labels: dict[str, GoldLabel]) -> None:
        self.labels = labels

    def classify(self, item: ClassificationInput) -> Classification | None:
        label = self.labels.get(item.number)
        if label is None:
            return None
        return Classification(category=label.category, method="gold", confidence="reviewed", quote=label.note)
