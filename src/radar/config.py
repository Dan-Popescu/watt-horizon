"""Runtime settings, from environment variables with safe defaults. No other module reads `os.environ`."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    offline: bool  # True: no network at all (lookups use the cache only)
    llm_model: str | None  # e.g. "claude-haiku-5-5"; None disables the LLM classifier
    use_tesseract: bool
    user_agent: str
    api_token: str | None = None  # if set, `radar web` requires `Authorization: Bearer <token>`

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
        return self.data_dir / "gold_labels.jsonl"


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    return Settings(
        data_dir=Path(os.environ.get("RADAR_DATA_DIR", "data")).resolve(),
        offline=_flag("RADAR_OFFLINE", False),
        llm_model=os.environ.get("RADAR_LLM_MODEL") or None,
        use_tesseract=_flag("RADAR_TESSERACT", True),
        user_agent=os.environ.get(
            "RADAR_USER_AGENT", "radar-raccordement/0.1 (+polite crawler; one request per second per host)"
        ),
        api_token=os.environ.get("RADAR_API_TOKEN") or None,
    )
