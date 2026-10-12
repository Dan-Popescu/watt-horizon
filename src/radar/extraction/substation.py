"""Which substation a project connects to: « poste RTE « Margot » », « poste source de SAUCOURT », « poste RTE 400kV »."""

import re

from radar.domain.text import squash

# Case-insensitive keywords, but the substation name itself must start with a capital letter (proper noun).
_POSTE = re.compile(
    r"(?i:poste(?:\s+(?:électrique|electrique|source))?(?:\s+de)?(?:\s+(?:RTE/Enedis|RTE|Enedis))?)\s*"
    r"(?:[«\"“]\s*(?P<quoted>[^»\"”]{2,40}?)\s*[»\"”]"
    r"|(?P<voltage>\d{2,3}\s?(?i:000\s?volts|kV))"
    r"|(?i:source|RTE)\s+(?i:de|d[’'])\s*(?P<named>[A-ZÉÈ][A-Za-zÀ-ÿ\-]{2,30}))"
)


def find_substation(text: str | None) -> tuple[str, str] | None:
    """Return (name or voltage, sentence) for the first substation mentioned, else None."""
    if not text:
        return None
    m = _POSTE.search(text)
    if not m:
        return None
    value = (m["quoted"] or m["voltage"] or m["named"] or "").strip()
    start = text.rfind(".", 0, m.start()) + 1
    end = text.find(".", m.end())
    sentence = squash(text[start : end if end > 0 else m.end() + 80])[:300]
    return value, sentence
