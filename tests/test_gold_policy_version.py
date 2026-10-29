"""A changed development reference invalidates derived classification output."""

from radar.classification.base import ClassificationInput
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category


def test_reference_policy_is_part_of_classifier_identity() -> None:
    key = "d:h:synthetic"
    old = GoldClassifier({key: GoldLabel("synthetic", Category.BESS, filing_key=key)})
    new = GoldClassifier({key: GoldLabel("synthetic", Category.DATACENTER, filing_key=key)})
    item = ClassificationInput(key, "synthetic", "Synthetic", "Synthetic")
    assert old.classify(item).category is Category.BESS
    assert new.classify(item).category is Category.DATACENTER
    assert old.name != new.name
