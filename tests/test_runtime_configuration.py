"""Resource and spending limits are explicit runtime settings, not implicit paid defaults."""

from pathlib import Path

import pytest

from radar.config import load_settings


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
