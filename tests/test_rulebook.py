"""Adopted rules: from a validated suggestion to a fixed case without code, evidence kept, bad rules dropped."""

from pathlib import Path

import pytest

from radar.pipeline.run import rule_filter
from radar.quality.arbiter import Arbiter
from radar.quality.cases import check
from radar.quality.rulebook import Rulebook, apply_rules
from radar.quality.suggest import suggest
from tests.test_improve import CASES

HTB = r"\bhtb\s+de\s+([a-z][a-z0-9' -]{1,40}?)(?=\s*(?:[,;.(]|$))"


def test_adopt_validates_and_persists(tmp_path: Path) -> None:
    book = Rulebook()
    rule = book.adopt("substation", HTB, origin="suggestion")
    assert book.adopt("substation", HTB).id == rule.id and len(book.rules) == 1  # idempotent
    for field, regex in [("color", HTB), ("substation", r"poste htb")]:
        with pytest.raises(ValueError):
            book.adopt(field, regex)
    book.save(tmp_path / "rules.json")
    assert Rulebook.load(tmp_path / "rules.json").rules == book.rules
    book.disable(rule.id)
    assert book.effective() == ()


def test_rule_values_carry_verbatim_evidence() -> None:
    rule = Rulebook().adopt("substation", HTB)
    hits = apply_rules([rule], {"d1": "Le projet sera raccordé au poste HTB de Vauxrot, situé à 2 km."})
    [hit] = hits["substation"]
    assert hit.value == "Vauxrot" and hit.candidate.source == f"rule:{rule.id}"  # as written, not folded
    assert hit.evidence.locator == f"rule:{rule.id}" and "poste HTB de Vauxrot" in hit.evidence.quote
    nothing = apply_rules([Rulebook().adopt("power_mw", r"puissance de (\d+) mw")], {"d1": "puissance de 12 kw"})
    assert nothing == {}  # no match, no value


def test_suggestion_adopted_fixes_cases_without_code() -> None:
    assert not check(CASES[0])["substation"]
    [best, *_] = suggest(CASES, "substation")
    rules = Rulebook()
    rules.adopt("substation", best.regex)
    adopted = rules.effective()
    assert all(check(c, adopted)["substation"] for c in CASES)  # the 2 failures fixed, the « absent » case intact
    assert suggest(CASES, "substation", rules=adopted) == []  # nothing left to fix


def test_unreliable_rules_are_dropped_by_reviews() -> None:
    book = Rulebook()
    rule = book.adopt("substation", HTB)
    good = Arbiter({("substation", f"rule:{rule.id}"): (9, 10)})
    bad = Arbiter({("substation", f"rule:{rule.id}"): (3, 10)})
    assert rule_filter(book.effective(), good) == (rule,)
    assert rule_filter(book.effective(), bad) == ()
    young = Arbiter({("substation", f"rule:{rule.id}"): (1, 4)})  # too few reviews to judge
    assert rule_filter(book.effective(), young) == (rule,)
