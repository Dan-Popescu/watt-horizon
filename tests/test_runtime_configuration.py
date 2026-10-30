"""Resource and spending limits are explicit runtime settings, not implicit paid defaults."""

from dataclasses import replace
from pathlib import Path

import pytest

from radar.config import Settings, load_settings
from radar.wiring import make_services


def test_paid_processing_is_closed_until_budgets_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("RADAR_LLM_DAILY_BUDGET_USD", "RADAR_LLM_CAMPAIGN_BUDGET_USD", "RADAR_LLM_FILING_BUDGET_USD"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.llm_daily_budget_usd == 0
    assert settings.llm_campaign_budget_usd == 0
    assert settings.llm_filing_budget_usd == 0
    assert settings.llm_jobs_path == settings.data_dir / "llm-jobs.sqlite"
    assert settings.llm_pricing_path is None


def test_resource_limits_are_configurable_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_PDF_WORKERS", "1")
    monkeypatch.setenv("RADAR_PDF_MAX_PAGES", "200")
    monkeypatch.setenv("RADAR_LLM_PRICING_PATH", "/tmp/pricing.json")
    settings = load_settings()
    assert settings.pdf_workers == 1
    assert settings.pdf_max_pages == 200
    assert settings.llm_pricing_path == Path("/tmp/pricing.json")
    monkeypatch.setenv("RADAR_PDF_WORKERS", "0")
    with pytest.raises(ValueError, match="RADAR_PDF_WORKERS"):
        load_settings()


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_invalid_budget_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("RADAR_LLM_DAILY_BUDGET_USD", value)
    with pytest.raises(ValueError, match="RADAR_LLM_DAILY_BUDGET_USD"):
        load_settings()


def test_reading_policy_is_opt_in_and_changes_processing_identity(settings: Settings) -> None:
    economical = make_services(settings, None, llm="cached")
    adaptive = make_services(replace(settings, reading_mode="adaptive"), None, llm="cached")
    limited = make_services(replace(settings, reading_mode="adaptive", reading_max_deep_pages=1), None, llm="cached")
    assert economical.reader.policy.mode == "economical"
    assert not economical.reader.read_tables
    assert adaptive.reader.policy.mode == "adaptive"
    assert adaptive.reader.read_tables
    assert adaptive.reader.version != limited.reader.version
    assert len({economical.fingerprint, adaptive.fingerprint, limited.fingerprint}) == 3
    explicit_tables = make_services(replace(settings, pdf_read_tables=True), None, llm="cached")
    assert explicit_tables.reader.read_tables


def test_reading_policy_environment_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_READING_MODE", "adaptive")
    monkeypatch.setenv("RADAR_READING_MAX_DEEP_PAGES", "3")
    monkeypatch.setenv("RADAR_READING_MAX_OCR_PAGES", "2")
    settings = load_settings()
    assert (settings.reading_mode, settings.reading_max_deep_pages, settings.reading_max_ocr_pages) == (
        "adaptive",
        3,
        2,
    )
    monkeypatch.setenv("RADAR_READING_MODE", "unknown")
    with pytest.raises(ValueError, match="RADAR_READING_MODE"):
        load_settings()
