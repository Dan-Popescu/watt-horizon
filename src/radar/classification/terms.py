"""Adopted screen terms: words added to the layer-1 screen WITHOUT code changes (`radar screen-terms adopt`),
versioned in `gold/screen_terms.json`. They come from screen misses (`quality/screen_terms.suggest`) and count as
strong clues on folded text (accent- and case-insensitive), for titles at collection time and full texts at build.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from radar.domain.text import fold


@dataclass
class ScreenTerm:
    id: str
    term: str  # folded words, e.g. "stockage thermique"
    status: str = "active"  # active | disabled
    created_on: str = ""
    origin: str = ""  # e.g. "rescues 2025-9147 (title); admits 2 of 412 rejected titles"


class ScreenTerms:
    def __init__(self, terms: list[ScreenTerm] | None = None) -> None:
        self.terms = terms or []

    @classmethod
    def load(cls, path: Path) -> "ScreenTerms":
        if not path.exists():
            return cls()
        return cls([ScreenTerm(**t) for t in json.loads(path.read_text(encoding="utf-8"))])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([asdict(t) for t in self.terms], ensure_ascii=False, indent=1) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")

    def adopt(self, term: str, origin: str = "") -> ScreenTerm:
        folded = fold(term)
        if len(folded) < 4:
            raise ValueError("term too short: it would match almost anything")
        found = next((t for t in self.terms if t.term == folded), None)
        if found is not None:
            found.status = "active"
            return found
        new = ScreenTerm(
            hashlib.sha1(folded.encode()).hexdigest()[:8], folded, "active", date.today().isoformat(), origin
        )
        self.terms.append(new)
        return new

    def disable(self, term_id: str) -> None:
        for t in self.terms:
            if t.id == term_id:
                t.status = "disabled"
                return
        raise ValueError(f"unknown term id {term_id}")

    def pattern(self) -> re.Pattern[str] | None:
        """One regex over folded text for every active term (whole words), None when there is none."""
        active = sorted({t.term for t in self.terms if t.status == "active"}, key=len, reverse=True)
        if not active:
            return None
        return re.compile(r"\b(?:" + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in active) + r")\b")
