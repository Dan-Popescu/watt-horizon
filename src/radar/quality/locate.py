"""Find where a human-verified value appears in a document text, with its surrounding words.

This turns a correction into evidence (a verbatim span) and into training material for rule suggestions
(quality/suggest.py): the words just before a value are what a rule must anchor on.
"""

import re
from dataclasses import dataclass

from radar.domain.text import fold

MONTHS_FR = (
    "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
)  # fmt: skip
CONTEXT = 80
KEPT = ".,/-;()"  # punctuation kept by fold_keep: numbers, dates, ends of names


@dataclass(frozen=True)
class Span:
    document_id: str
    start: int
    end: int
    text: str  # the matched text, verbatim
    left: str  # up to CONTEXT characters before
    right: str  # up to CONTEXT characters after


def _number_pattern(value: float) -> str:
    whole, _, decimals = f"{value:g}".partition(".")
    grouped = r"[\s  ]?".join(whole) if len(whole) > 3 else whole  # « 1 292 »
    frac = rf"[.,]{decimals}0*" if decimals else r"(?:[.,]0+)?"
    return rf"(?<![\d.,]){grouped}{frac}(?![\d]|[.,]\d)"  # 47 must not match inside 47,5


def _date_patterns(iso: str) -> list[str]:
    y, m, d = iso.split("-")
    day, month = str(int(d)), MONTHS_FR[int(m) - 1]
    return [
        rf"\b0?{day}(?:er)?\s+{month}\s+{y}\b",  # matched on folded text
        rf"\b{d}[\s/.-]{m}[\s/.-]{y}\b",
        rf"\b{y}-{m}-{d}\b",
    ]


def patterns(field: str, value: str | float) -> list[str]:
    """Regexes (applied to FOLDED text) that find this value as it may be written in a document."""
    if field in {"power_mw", "power_mw_min", "energy_mwh"}:
        return [_number_pattern(float(value))]
    if field == "filed_on":
        return _date_patterns(str(value))
    if field == "siret":
        digits = re.sub(r"\D", "", str(value))
        return [r"\s?".join(digits)]
    words = fold(str(value)).split()
    return [r"\b" + r"[\s\-']+".join(re.escape(w) for w in words) + r"\b"] if words else []


def _folded_with_map(text: str) -> tuple[str, list[int]]:
    """Folded text + the original index of each folded character (fold() collapses accents and punctuation)."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        # Keep the punctuation numbers, dates and compound names are written with; fold the rest.
        f = ch if ch in KEPT else "'" if ch in "'’" else (fold(ch) or " ")
        for c in f:
            out.append(c)
            index.append(i)
    return "".join(out), index


def fold_keep(text: str) -> str:
    """fold() that keeps the punctuation numbers and names need (« 47,5 », « 11-02-2026 », « Famars (59) »)."""
    return _folded_with_map(text)[0]


def locate(field: str, value: str | float | None, texts: dict[str, str], limit: int = 5) -> list[Span]:
    """Spans of `value` in the documents (`texts`: document id -> text), most specific patterns first."""
    if value in (None, ""):
        return []
    spans: list[Span] = []
    for doc_id, text in texts.items():
        folded, index = _folded_with_map(text)
        for pattern in patterns(field, value):
            for m in re.finditer(pattern, folded):
                if m.end() == m.start():
                    continue
                start, end = index[m.start()], index[m.end() - 1] + 1
                spans.append(
                    Span(
                        doc_id,
                        start,
                        end,
                        text[start:end],
                        text[max(0, start - CONTEXT) : start],
                        text[end : end + CONTEXT],
                    )
                )
                if len(spans) >= limit:
                    return spans
    return spans
