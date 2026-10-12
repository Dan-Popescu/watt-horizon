"""Layer 1: a broad keyword screen. Goal: ~100 % recall (missing a project costs more than a false alarm).

Never screen on titles alone: DREAL titles are truncated (« sur la commune de (…) »). Pass every text you have.
"""

import re
from dataclasses import dataclass

STRONG = re.compile(
    r"stockage (?:d.?)?(?:é|e)lectricit|stockage d.?(?:é|e)nergie|infrastructures de stockage \(|batteries?\b|\bBESS\b"
    r"|\bMWh\b|conteneurs? (?:de )?batter|data ?cent|centre de donn|h(?:é|e)bergement (?:de donn|informatique)"
    r"|serveurs informatiques",
    re.IGNORECASE,
)
WEAK = re.compile(
    r"poste (?:source|(?:é|e)lectrique|de transformation|priv(?:é|e)|de livraison|\d{2,3})|cr(?:é|e)ation d.un poste"
    r"|\b\d{2,3} ?000(?: ?/ ?\d{2,3} ?000)? ?volts|\b(?:63|90|225|400) ?kV\b"
    r"|rubrique\s*32\b|\b2925\b|\b2910\b|\b3110\b|groupes? (?:é|e)lectrog",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Screening:
    keep: bool
    score: int  # 3 per strong clue + 1 per weak clue
    clues: tuple[str, ...]


def screen(*texts: str | None, max_chars: int = 20_000) -> Screening:
    blob = " ".join(t for t in texts if t)[:max_chars]
    strong = sorted({m[0].lower() for m in STRONG.finditer(blob)})
    weak = sorted({m[0].lower() for m in WEAK.finditer(blob)})
    return Screening(keep=bool(strong or weak), score=3 * len(strong) + len(weak), clues=tuple(strong + weak))
