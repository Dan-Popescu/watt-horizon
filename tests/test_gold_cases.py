"""Replays every regression case of the repository (gold/cases). Fixed fields must pass; open ones are known
errors (xfail) until the rules are improved and `radar cases check` marks them fixed."""

from pathlib import Path

import pytest

from radar.quality.cases import check, load_all

CASES = load_all(Path(__file__).parent.parent / "gold" / "cases")
PARAMS = [pytest.param(case, name, id=f"{case.number}:{name}") for case in CASES for name in case.expected]


@pytest.mark.parametrize(("case", "name"), PARAMS)
def test_case(case, name) -> None:  # type: ignore[no-untyped-def]
    ok = check(case)[name]
    if case.status.get(name) != "fixed":
        if not ok:
            pytest.xfail("open case: known extraction error, see gold/cases")
        return
    assert ok, f"regression on {case.number}:{name} (expected {case.expected[name]!r})"
