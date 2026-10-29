"""Regression cases with reused administrative numbers never overwrite one another."""

import json
from pathlib import Path

import pytest

from radar.quality.cases import Case, case_path, load, load_all, refresh, save


def test_same_number_in_two_sources_is_preserved_independently(tmp_path: Path) -> None:
    first = Case("42", {"substation": None}, [], filing_key="dreal:north:42")
    second = Case("42", {"substation": None}, [], filing_key="mrae:north:42")
    paths = [save(first, tmp_path), save(second, tmp_path)]
    assert paths[0] != paths[1]
    assert load(paths[0]) == first
    assert load(paths[1]) == second
    assert {case.filing_key for case in load_all(tmp_path)} == {first.filing_key, second.filing_key}
    result = refresh(tmp_path)
    assert set(result["newly_fixed"]) == {"dreal:north:42:substation", "mrae:north:42:substation"}


def test_legacy_case_without_filing_key_keeps_filename_and_status_label(tmp_path: Path) -> None:
    path = tmp_path / "2026-42.json"
    path.write_text(json.dumps({"number": "2026-42", "expected": {"substation": None}, "documents": []}))
    legacy = load(path)
    assert legacy.filing_key is None
    assert case_path(legacy, tmp_path) == path
    assert refresh(tmp_path)["newly_fixed"] == ["2026-42:substation"]


@pytest.mark.parametrize("number", ["../outside", "folder/file", "..", "bad.case", ""])
def test_unsafe_legacy_number_is_refused(number: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="number"):
        save(Case(number, {}, []), tmp_path)


def test_canonical_filename_is_stable_and_cannot_escape_directory(tmp_path: Path) -> None:
    case = Case("../unsafe-display-number", {}, [], filing_key="dreal:north:42")
    path = case_path(case, tmp_path)
    assert path.parent == tmp_path
    assert path == case_path(case, tmp_path)
    assert save(case, tmp_path) == path
