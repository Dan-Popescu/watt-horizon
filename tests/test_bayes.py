"""Second-opinion classifier: names never enter the vocabulary; it only speaks once proven reliable."""

import random

from radar.classification.bayes import MIN_LABELS, train, vocabulary
from radar.domain.enums import Category
from radar.domain.models import Classification
from radar.extraction.facts import Facts
from radar.quality.flags import QualityInput, detect

BESS = "stockage batteries conteneurs onduleurs poste livraison mwh capacité réseau"
DC = "centre données serveurs refroidissement salles informatiques groupes électrogènes réseau"
OTHER = "carrière extraction matériaux remblai défrichement voirie réseau"


def _examples(n: int, seed: int = 0) -> list[tuple[tuple[str, ...], Category]]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        cat, words = [(Category.BESS, BESS), (Category.DATACENTER, DC), (Category.OTHER, OTHER)][i % 3]
        kept = [w for w in words.split() if rng.random() < 0.7] + ["projet", "commune"]
        out.append((vocabulary(" ".join(kept)), cat))
    return out


def test_vocabulary_keeps_lowercase_words_and_acronyms_only() -> None:
    words = vocabulary("Projet BESS de 40 MWh porté par Jean DUPONT à Famars : stockage par batteries, les conteneurs")
    assert {"bess", "mwh", "stockage", "batteries", "conteneurs"} <= set(words)
    assert not {"jean", "dupont", "famars", "projet", "les", "par"} & set(words)


def test_leave_one_out_and_prediction() -> None:
    model = train(_examples(45))
    assert model is not None and model.loo_total == 45 and model.reliable
    opinion = model.predict(vocabulary("serveurs et refroidissement des salles informatiques"))
    assert opinion is not None and opinion.category is Category.DATACENTER and opinion.probability > 0.9
    assert "serveurs" in opinion.clues


def test_not_reliable_with_few_labels() -> None:
    model = train(_examples(MIN_LABELS - 3))
    assert model is not None and not model.reliable
    assert train([(("batteries",), Category.BESS)] * 40) is None  # one category: nothing to compare


def test_confident_disagreement_is_flagged() -> None:
    model = train(_examples(45))
    assert model is not None
    opinion = model.predict(vocabulary(BESS))
    q = QualityInput(
        documents=[],
        form=Facts(),
        decision=Facts(),
        facts=Facts(),
        classification=Classification(category=Category.OTHER, method="rules"),
        checks=(),
        target=False,
        kept_by_text=True,
        unread=[],
        second_opinion=opinion,
    )
    codes = {f.code for f in detect(q)}
    assert "second_opinion_disagrees" in codes
    same = Classification(category=Category.BESS, method="gold")
    assert "second_opinion_disagrees" not in {
        f.code for f in detect(q.__class__(**{**q.__dict__, "classification": same}))
    }
