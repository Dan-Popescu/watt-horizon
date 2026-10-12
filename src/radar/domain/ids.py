"""Stable identifiers. Filing numbers appear in many spellings across documents of the same filing."""

import hashlib
import re

from radar.domain.enums import SourceKind


def normalize_filing_number(raw: str | None) -> str | None:
    """'005382 KKP' / '010504/KK P' / '0010504-cerfa.pdf' -> '005382' / '010504' / '010504';
    '2025-8646' / '2025_4009' / '2025-8646-decision.pdf' -> '2025-8646' / '2025-4009' / '2025-8646'."""
    if not raw:
        return None
    text = raw.replace("_", "-")
    if m := re.search(r"\b(20\d\d)-(\d{3,5})\b", text) or re.search(r"(20\d\d)-(\d{3,5})", text):
        return f"{m[1]}-{m[2]}"
    if m := re.search(r"0*(\d{6})(?!\d)", text):
        return m[1]
    return None


def filing_key(source: SourceKind, region: str, number: str) -> str:
    return f"{source.value}:{region}:{number}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_id(filing_key_value: str) -> str:
    """Short, stable project id derived from its founding filing."""
    return "p_" + hashlib.sha1(filing_key_value.encode("utf-8")).hexdigest()[:12]


def luhn_ok(number: str) -> bool:
    """SIRET/SIREN check digit (Luhn)."""
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def find_siret(text: str) -> re.Match[str] | None:
    """First 14-digit number with a valid check digit (labels are often far from values in flattened forms)."""
    return next((m for m in re.finditer(r"(?<!\d)(\d{14})(?!\d)", text) if luhn_ok(m[1])), None)
