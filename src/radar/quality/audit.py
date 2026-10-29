"""What to fetch although the title screen rejects it: the only way to learn what the screen misses.

- near-miss: the title suggests a hidden target (solar/wind project: storage may be coupled; industrial change of an
  unknown nature; title cut before the project nature). The full document is then screened again on its text.
- random: a stable pseudo-random share (`AUDIT_RATE`) of every rejected item. Reviewing these gives an UNBIASED
  estimate of the screen's recall (near-miss items are chosen, hence biased; they find errors, not rates).
Both are recorded on the document (`DocumentRecord.audit`) and in `listing_items` (denominator for the estimate).
"""

import hashlib
import re

from radar.classification.rules import screen

AUDIT_RATE = 0.03
AUDIT_POLICY_VERSION = 2
NEAR_MISS: dict[str, re.Pattern[str]] = {
    "solar-wind": re.compile(r"(?i)solaire|photovolta|agrivolta|(?:é|e)olien"),
    "industrial-change": re.compile(r"(?i)\b(?:modification|extension|augmentation|r(?:é|e)organisation)\b.*\(…\)"),
    "cut-before-nature": re.compile(
        r"(?i)(?:projet|relatif|portant sur)\s+(?:de\s+|d[’']\s*|du\s+|à\s+)?(?:la\s+|l[’']\s*|un\s+|une\s+)?\(…\)\s*$"
    ),
}


def stable_fraction(key: str) -> float:
    """Deterministic value in [0, 1) from a key: the same item is always in (or out of) the random sample."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 2**32


def near_miss(title: str) -> str | None:
    return next((name for name, pattern in NEAR_MISS.items() if pattern.search(title)), None)


def decide(
    title: str, key: str, rate: float = AUDIT_RATE, extra: re.Pattern[str] | None = None
) -> tuple[bool, str | None]:
    """(kept by the screen, audit reason). Fetch when kept or when an audit reason is given.
    `extra`: adopted screen terms (gold/screen_terms.json)."""
    if screen(title, extra=extra).keep:
        return True, None
    # Sample every rejected item, including near-misses, before targeted error discovery.
    if stable_fraction(key) < rate:
        return False, "random"
    if reason := near_miss(title):
        return False, f"near-miss:{reason}"
    return False, None
