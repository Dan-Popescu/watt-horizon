"""Small text helpers used across layers."""

import re
import unicodedata

_SPACES = re.compile(r"\s+")


def squash(text: str | None) -> str:
    """Collapse all whitespace (including non-breaking spaces) to single spaces."""
    return _SPACES.sub(" ", (text or "").replace(" ", " ").replace(" ", " ")).strip()


def fold(text: str | None) -> str:
    """Lowercase, strip accents and punctuation: for comparisons only, never for display."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    no_accents = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", no_accents).strip()


def contains_quote(haystack: str, quote: str) -> bool:
    """True if `quote` appears in `haystack`, ignoring whitespace and case differences."""
    q = squash(quote).lower()
    return bool(q) and q in squash(haystack).lower()


def excerpt(text: str, start: int, end: int, margin: int = 80, limit: int = 300) -> str:
    """Readable excerpt around text[start:end], used as evidence."""
    return squash(text[max(0, start - margin) : end + margin])[:limit]


def parse_number(raw: str) -> float:
    """'1 292,5' -> 1292.5"""
    return float(raw.replace(" ", "").replace(" ", "").replace(" ", "").replace(",", "."))
