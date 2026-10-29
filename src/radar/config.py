"""Runtime settings, from environment variables with safe defaults. No other module reads `os.environ`."""

import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    offline: bool  # True: no network at all (lookups use the cache only)
    llm_model: str | None  # e.g. "claude-haiku-5-5"; None disables the LLM classifier
    use_tesseract: bool
    user_agent: str
    api_token: str | None = None  # if set, `radar web` requires `Authorization: Bearer <token>`
    llm_fast_model: str | None = None  # e.g. claude-haiku-5-5: classify first, escalate when doubtful (measure first)
    llm_passages: bool = False  # extraction sends windows around clues instead of the first 20k chars (measure first)
    llm_check: bool = True  # model pre-review of uncertain values (needs llm_model; uses the fast model if set)
    gold_dir: Path = Path("gold")  # reference sets and review exports (in the repository, not in data/)
    pdf_workers: int = 2
    http_max_bytes: int = 32 * 1024 * 1024
    http_deadline_s: int = 60
    pdf_max_bytes: int = 32 * 1024 * 1024
    pdf_max_pages: int = 500
    pdf_batch_bytes: int = 64 * 1024 * 1024
    pdf_timeout_s: int = 30
    ocr_max_pages: int = 100
    ocr_max_pixels: int = 40_000_000
    ocr_timeout_s: int = 60
    llm_daily_budget_usd: float = 0.0
    llm_campaign_budget_usd: float = 0.0
    llm_filing_budget_usd: float = 0.0
    llm_max_attempts: int = 3
    llm_campaign: str = "default"
    llm_pricing_path: Path | None = None
    llm_wait_timeout_s: int = 3600
    llm_poll_interval_s: int = 10
    published_dir: Path | None = None
    evaluation_campaign: str = "default"
    evaluation_test_after: date | None = None
    source_catalog_path: Path | None = None
    pdf_read_tables: bool = False
    pdf_table_max_pages: int = 20
    pdf_table_max_rows: int = 2000
    pdf_table_max_chars: int = 200_000
    ocr_secondary_pass: bool = False
    ocr_secondary_max_pages: int = 8

    @property
    def catalog_path(self) -> Path:
        return self.source_catalog_path or self.data_dir / "sources" / "catalog.json"

    @property
    def llm_jobs_path(self) -> Path:
        """Durable requests and usage; deliberately outside the disposable cache."""
        return self.data_dir / "llm-jobs.sqlite"

    @property
    def partitions_path(self) -> Path:
        return self.evaluation_dir / "partitions.json"

    @property
    def baseline_path(self) -> Path:
        return self.evaluation_dir / "approved.json"

    @property
    def evaluation_dir(self) -> Path:
        base = self.data_dir / "evaluation"
        return base if self.evaluation_campaign == "default" else base / "campaigns" / self.evaluation_campaign

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.sqlite"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def transcripts_dir(self) -> Path:
        """Verbatim transcriptions of scanned documents: `<document_id>.txt`."""
        return self.data_dir / "transcripts"

    @property
    def lookup_cache_path(self) -> Path:
        return self.data_dir / "cache" / "lookups.json"

    @property
    def cache_path(self) -> Path:
        """Disposable cache of expensive derived results (OCR pages, LLM answers)."""
        return self.data_dir / "cache" / "derived.sqlite"

    @property
    def communes_path(self) -> Path:
        """All French communes with centroids (geo.api.gouv.fr export), used offline."""
        return self.data_dir / "reference" / "communes.json"

    @property
    def gold_path(self) -> Path:
        """Category labels per filing (classification gold). Versioned in git: human work."""
        return self.gold_dir / "labels.jsonl"

    @property
    def field_gold_path(self) -> Path:
        """Expected field values per filing (extraction gold). Versioned in git."""
        return self.gold_dir / "fields.jsonl"

    @property
    def cases_dir(self) -> Path:
        """Regression cases built from human-verified filings (`radar cases export`)."""
        return self.gold_dir / "cases"

    @property
    def rules_path(self) -> Path:
        """Adopted extraction rules (`radar rules adopt`), applied at every build as arbiter candidates."""
        return self.gold_dir / "rules.json"

    @property
    def screen_terms_path(self) -> Path:
        """Adopted screen terms (`radar screen-terms adopt`), added to the layer-1 screen."""
        return self.gold_dir / "screen_terms.json"

    @property
    def templates_path(self) -> Path:
        """Known document templates (`radar templates export`); unknown ones are flagged at build."""
        return self.gold_dir / "templates.json"

    @property
    def aliases_path(self) -> Path:
        """Verified spellings of names learned from corrections (`radar aliases export`)."""
        return self.gold_dir / "aliases.json"

    @property
    def link_decisions_path(self) -> Path:
        """Export of human link decisions (`radar review export`): same project / related / unrelated."""
        return self.gold_dir / "link_decisions.jsonl"

    @property
    def corrections_path(self) -> Path:
        """Export of human corrections (`radar review export`), so reviews survive a lost database."""
        return self.gold_dir / "corrections.jsonl"


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _budget(name: str) -> float:
    try:
        value = float(os.environ.get(name, "0"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite non-negative USD limit") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative USD limit")
    return value


def _optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).resolve() if value else None


def _evaluation_campaign() -> str:
    value = os.environ.get("RADAR_EVALUATION_CAMPAIGN", "default")
    if (
        not value
        or len(value) > 80
        or value in {".", ".."}
        or not value.isascii()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError("RADAR_EVALUATION_CAMPAIGN must be a simple ASCII campaign name")
    return value


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("RADAR_DATA_DIR", "data")).resolve(),
        offline=_flag("RADAR_OFFLINE", False),
        llm_model=os.environ.get("RADAR_LLM_MODEL") or None,
        use_tesseract=_flag("RADAR_TESSERACT", True),
        user_agent=os.environ.get(
            "RADAR_USER_AGENT", "watt-horizon/0.1 (+polite crawler; one request per second per host)"
        ),
        api_token=os.environ.get("RADAR_API_TOKEN") or None,
        llm_fast_model=os.environ.get("RADAR_LLM_FAST_MODEL") or None,
        llm_passages=_flag("RADAR_LLM_PASSAGES", False),
        llm_check=_flag("RADAR_LLM_CHECK", True),
        gold_dir=Path(os.environ.get("RADAR_GOLD_DIR", "gold")).resolve(),
        pdf_workers=_positive_int("RADAR_PDF_WORKERS", 2),
        http_max_bytes=_positive_int("RADAR_HTTP_MAX_BYTES", 32 * 1024 * 1024),
        http_deadline_s=_positive_int("RADAR_HTTP_DEADLINE_S", 60),
        pdf_max_bytes=_positive_int("RADAR_PDF_MAX_BYTES", 32 * 1024 * 1024),
        pdf_max_pages=_positive_int("RADAR_PDF_MAX_PAGES", 500),
        pdf_batch_bytes=_positive_int("RADAR_PDF_BATCH_BYTES", 64 * 1024 * 1024),
        pdf_timeout_s=_positive_int("RADAR_PDF_TIMEOUT_S", 30),
        ocr_max_pages=_positive_int("RADAR_OCR_MAX_PAGES", 100),
        ocr_max_pixels=_positive_int("RADAR_OCR_MAX_PIXELS", 40_000_000),
        ocr_timeout_s=_positive_int("RADAR_OCR_TIMEOUT_S", 60),
        llm_daily_budget_usd=_budget("RADAR_LLM_DAILY_BUDGET_USD"),
        llm_campaign_budget_usd=_budget("RADAR_LLM_CAMPAIGN_BUDGET_USD"),
        llm_filing_budget_usd=_budget("RADAR_LLM_FILING_BUDGET_USD"),
        llm_max_attempts=_positive_int("RADAR_LLM_MAX_ATTEMPTS", 3),
        llm_campaign=os.environ.get("RADAR_LLM_CAMPAIGN", "default"),
        llm_pricing_path=_optional_path("RADAR_LLM_PRICING_PATH"),
        llm_wait_timeout_s=_positive_int("RADAR_LLM_WAIT_TIMEOUT_S", 3600),
        llm_poll_interval_s=_positive_int("RADAR_LLM_POLL_INTERVAL_S", 10),
        published_dir=_optional_path("RADAR_PUBLISHED_DIR"),
        source_catalog_path=_optional_path("RADAR_SOURCE_CATALOG"),
        pdf_read_tables=_flag("RADAR_PDF_TABLES", False),
        pdf_table_max_pages=_positive_int("RADAR_PDF_TABLE_MAX_PAGES", 20),
        pdf_table_max_rows=_positive_int("RADAR_PDF_TABLE_MAX_ROWS", 2000),
        pdf_table_max_chars=_positive_int("RADAR_PDF_TABLE_MAX_CHARS", 200_000),
        ocr_secondary_pass=_flag("RADAR_OCR_SECONDARY", False),
        ocr_secondary_max_pages=_positive_int("RADAR_OCR_SECONDARY_MAX_PAGES", 8),
        evaluation_campaign=_evaluation_campaign(),
        evaluation_test_after=date.fromisoformat(os.environ["RADAR_EVAL_TEST_AFTER"])
        if os.environ.get("RADAR_EVAL_TEST_AFTER")
        else None,
    )
