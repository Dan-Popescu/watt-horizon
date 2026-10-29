"""Presentation edits do not invalidate extraction; semantic code and local corrections do."""

from pathlib import Path

import pytest

from radar.pipeline import fingerprint


def test_presentation_change_does_not_reprocess_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fingerprint, "PACKAGE", tmp_path)
    (tmp_path / "extraction").mkdir()
    semantic = tmp_path / "extraction" / "decision.py"
    semantic.write_text("VERSION = 1\n")
    (tmp_path / "web").mkdir()
    page = tmp_path / "web" / "app.py"
    page.write_text("LABEL = 'old'\n")
    before = fingerprint.processing_fingerprint()
    page.write_text("LABEL = 'new'\n")
    assert fingerprint.processing_fingerprint() == before
    semantic.write_text("VERSION = 2\n")
    assert fingerprint.processing_fingerprint() != before
