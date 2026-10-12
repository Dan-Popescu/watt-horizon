"""GDPR minimisation: remove natural persons from every text we store or send to a model.

Sources of person names in filings:
- form fields for the representative, the signatory, or a natural-person applicant (harvested per filing);
- civility + name in prose (« Monsieur Jean DUPONT », « Mme X »);
- e-mail addresses and phone numbers.
Applied to evidence quotes, descriptions, titles and LLM inputs. Company names are never masked.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from radar.domain.text import fold

MASK = "[nom masqué]"
EMAIL_MASK = "[courriel]"
PHONE_MASK = "[téléphone]"

# Folded field names: « NomPersonneMorale1 », « PrenomPorteurProjet », « 2.2. Nom », « 9. Prenom », « nomPersonnePhysique ».
_PERSON_FIELD = re.compile(r"^(?:[\d ]+)?(?:nom(?!enclature)|prenom)|personnephysique")
_CIVILITY = re.compile(
    r"\b(?P<civ>M\.|Mme|Mlle|Monsieur|Madame|Mademoiselle)\s+"
    r"(?P<name>(?:[A-ZÉÈÊÎÔ][A-Za-zÀ-ÿ'’\-]+\.?\s?){1,3})"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Pairs with one consistent separator, or 10 digits in a row; « 05 11 2024 2024_8373 » (date + case number) is not a phone.
_PHONE = re.compile(r"(?<![\d_-])(?:\+33\s?|0)[1-9](?:(?P<sep>[ .-])\d{2}(?:(?P=sep)\d{2}){3}|\d{8})(?![\d_-])")
_REPEATED_MASK = re.compile(rf"{re.escape(MASK)}(?:\s*{re.escape(MASK)})+")
_LABELS = frozenset({"nom", "prenom", "prenoms", "s", "madame", "monsieur"})  # form labels, not names


def is_person_field(name: str) -> bool:
    return bool(_PERSON_FIELD.search(fold(name).replace(" ", "")))


def person_names(fields: Mapping[str, str]) -> set[str]:
    """Name tokens (3+ letters) found in the person fields of a form; form labels excluded."""
    names: set[str] = set()
    for key, value in fields.items():
        if value and is_person_field(key):
            for token in re.findall(r"[A-Za-zÀ-ÿ'’\-]{3,}", value):
                token = token.strip("-'’")
                if len(token) >= 3 and fold(token) not in _LABELS:
                    names.add(token)
    return names


@dataclass(frozen=True)
class PersonScrubber:
    """Masks the given name tokens (whole words, case-insensitive) plus civility names, e-mails and phones."""

    names: frozenset[str] = frozenset()
    keep: frozenset[str] = frozenset()  # folded tokens that must stay visible (company names)
    _pattern: re.Pattern[str] | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        tokens = sorted({n for n in self.names if fold(n) not in self.keep}, key=len, reverse=True)
        if tokens:
            alternatives = "|".join(re.escape(t) for t in tokens)
            object.__setattr__(self, "_pattern", re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE))

    @classmethod
    def for_filing(cls, forms: Iterable[Mapping[str, str]], companies: Iterable[str | None] = ()) -> "PersonScrubber":
        names: set[str] = set()
        for f in forms:
            names |= person_names(f)
        keep = {token for c in companies if c for token in fold(c).split()}
        return cls(frozenset(names), frozenset(keep))

    def scrub(self, text: str | None) -> str | None:
        if text is None:
            return None
        text = _EMAIL.sub(EMAIL_MASK, text)
        text = _PHONE.sub(PHONE_MASK, text)
        text = _CIVILITY.sub(lambda m: f"{m['civ']} {MASK}" + (" " if m[0][-1].isspace() else ""), text)
        if self._pattern is not None:
            text = self._pattern.sub(MASK, text)
        return _REPEATED_MASK.sub(MASK, text)

    def leaks(self, text: str) -> list[str]:
        """Person data still present in `text` (used by the privacy audit)."""
        found = [m[0] for m in _EMAIL.finditer(text)] + [m[0] for m in _PHONE.finditer(text)]
        found += [m["name"].strip() for m in _CIVILITY.finditer(text) if MASK not in m["name"]]
        if self._pattern is not None:
            found += [m[0] for m in self._pattern.finditer(text)]
        return found
