"""Shared connector types."""

from dataclasses import dataclass, field


@dataclass
class CollectReport:
    seen: int = 0  # items listed by the source
    kept: int = 0  # items passing the screen
    documents: int = 0  # files ingested
    signals: int = 0  # signals stored
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None  # set when a site blocked us: the run stopped early on purpose
