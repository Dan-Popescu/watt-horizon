"""Which substation a project connects to.

Seen in filings: « poste RTE « Margot » », « poste source de SAUCOURT », « poste source de La Capelle »,
« poste électrique existant des Mandarins », « poste électrique RTE 400kV/225kV de Famars », « poste RTE 400kV ».
A name beats a bare voltage, wherever it appears: the first mention is often generic (« poste RTE 400kV »).
"""

import re

from radar.domain.text import fold, squash

# Keywords are case-insensitive; a qualifier is required so « poste de livraison » or « poste de travail » never match.
_KEYWORDS = (
    r"(?i:poste\s+(?:(?:électrique|electrique|source)(?:\s+(?:RTE/Enedis|RTE|Enedis))?|RTE/Enedis|RTE|Enedis)"
    r"(?:\s+existant)?)"
)
_VOLTAGE = r"\d{2,3}\s?(?i:000\s?volts|kV)(?:\s?/\s?\d{2,3}\s?(?i:kV))?"
# Proper noun, case-sensitive: « Famars », « SAUCOURT », « La Capelle », « Lislet 2 », « CHEMIN DE VAILLY ».
# Words join only through a connector (de, sur…), so « SEQUEDIN ENEDIS » stops at « SEQUEDIN ».
_WORD = r"(?:(?i:la|le|les)\s+|(?i:l)[’'])?(?!(?i:poste)\b)[A-ZÉÈÎ][A-Za-zÀ-ÿ\-]{1,30}"
_NAME = _WORD + r"(?:\s(?i:de|du|des|sur|sous|lès|les)\s+" + _WORD + r")*(?:\s\d{1,2}\b)?"
# Words that are never a substation name (all-caps documents make every word look like a proper noun).
GENERIC = frozenset(
    {"transformation", "livraison", "source", "electrique", "raccordement", "poste", "commune", "zone", "parcelle",
     "site", "projet", "ville", "region", "societe", "rte", "enedis", "la", "le", "les", "amont", "aval", "reseau"}
)  # fmt: skip


def _generic(name: str) -> bool:
    words = [w for w in fold(name).split() if w not in {"la", "le", "les", "l"}]
    return not words or words[0] in GENERIC


_POSTE = re.compile(
    _KEYWORDS + r"\s*(?:"
    r"(?i:de\s+)?[«\"“]\s*(?P<quoted>[^»\"”]{2,40}?)\s*[»\"”]"
    r"|(?:" + _VOLTAGE + r"\s+)?(?i:de|des|du|d[’'])\s*(?P<named>" + _NAME + r")"
    r"|(?i:de\s+)?(?P<voltage>" + _VOLTAGE + r"))"
)
_RANK = {"quoted": 0, "named": 1, "voltage": 2}


def find_substation(*texts: str | None) -> tuple[str, str] | None:
    """Return (name or voltage, sentence) for the most specific substation mention across `texts`, else None."""
    best: tuple[int, str, str] | None = None
    for text in texts:
        for m in _POSTE.finditer(text or ""):
            kind = next(k for k in _RANK if m[k])
            if kind == "named" and _generic(m[kind]):
                continue
            if best is None or _RANK[kind] < best[0]:
                best = (_RANK[kind], m[kind].strip(), _sentence(text or "", m.start(), m.end()))
                if best[0] == 0:
                    return best[1], best[2]  # a quoted name cannot be beaten
    return (best[1], best[2]) if best else None


def is_voltage_only(value: str | None) -> bool:
    """True for « 400kV »-style values, which tell the voltage but not which substation."""
    return bool(value) and re.fullmatch(_VOLTAGE, value or "") is not None


def _sentence(text: str, start: int, end: int) -> str:
    left = text.rfind(".", 0, start) + 1
    right = text.find(".", end)
    return squash(text[left : right if right > 0 else end + 80])[:300]
