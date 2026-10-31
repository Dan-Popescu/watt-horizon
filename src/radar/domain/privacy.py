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
MASKED_PERSON = "Personne physique (nom masqué)"  # developer value when the applicant is a natural person
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
_COMPANY_MARKERS = re.compile(
    r"\b(?:sas|sasu|sarl|sa|sci|scea|earl|gaec|snc|eurl)\b|energ|power|storage|solar|stockage|poste|data|group|france"
    r"|assets|industr|wind|sun|capital|invest|reseau|réseau|\d",
    re.IGNORECASE,
)
_PERSON = re.compile(
    r"^(?:M\.|Mme|Monsieur|Madame)?\s*[A-ZÉ][a-zéèëï]+(?:-[A-ZÉ][a-zéèëï]+)?\s+[A-ZÉ][A-Za-zéèëï\-]+$",
)
_ACCENT_VARIANTS = {
    "a": "aàáâäãå",
    "c": "cç",
    "e": "eèéêë",
    "i": "iìíîï",
    "n": "nñ",
    "o": "oòóôöõ",
    "u": "uùúûü",
    "y": "yýÿ",
}


def _company_pattern(name: str) -> str:
    return r"[\W_]+".join(
        "".join(f"[{_ACCENT_VARIANTS[letter]}]" if letter in _ACCENT_VARIANTS else re.escape(letter) for letter in word)
        for word in fold(name).split()
    )


def looks_like_person(name: str | None) -> bool:
    """Conservative applicant-name heuristic shared by minimisation and validation."""
    return bool(name) and bool(_PERSON.match(name or "")) and not _COMPANY_MARKERS.search(name or "")


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
    keep: frozenset[str] = frozenset()  # complete company names, never globally exempted person tokens
    _pattern: re.Pattern[str] | None = field(default=None, init=False, compare=False, repr=False)
    _keep_pattern: re.Pattern[str] | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        tokens = sorted(self.names, key=len, reverse=True)
        if tokens:
            alternatives = "|".join(re.escape(t) for t in tokens)
            object.__setattr__(self, "_pattern", re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE))
        if self.keep:
            companies = "|".join(
                _company_pattern(name) for name in sorted(self.keep, key=len, reverse=True) if fold(name)
            )
            object.__setattr__(self, "_keep_pattern", re.compile(rf"(?<![\w-])(?:{companies})(?![\w-])", re.IGNORECASE))

    @classmethod
    def for_filing(
        cls,
        forms: Iterable[Mapping[str, str]],
        companies: Iterable[str | None] = (),
        *,
        people: Iterable[str | None] = (),
    ) -> "PersonScrubber":
        names: set[str] = set()
        for f in forms:
            names |= person_names(f)
        for person in people:
            names |= person_names({"nom": person or ""})
        keep: set[str] = set()
        for company in companies:
            company_tokens = set(fold(company).split()) if company else set()
            known_person = company_tokens and company_tokens <= {
                token for name in names for token in fold(name).split()
            }
            if known_person:
                # An extracted denomination may actually be a natural-person applicant.
                # It must not exempt that person's tokens before validation runs.
                names |= person_names({"nom": company or ""})
            elif company:
                keep.add(company)
        return cls(frozenset(names), frozenset(keep))

    def _person_matches(self, text: str) -> list[re.Match[str]]:
        if self._pattern is None:
            return []
        protected = [match.span() for match in self._keep_pattern.finditer(text)] if self._keep_pattern else []
        result: list[re.Match[str]] = []
        index = 0
        for match in self._pattern.finditer(text):
            while index < len(protected) and protected[index][1] <= match.start():
                index += 1
            if index < len(protected) and protected[index][0] <= match.start() and match.end() <= protected[index][1]:
                continue
            result.append(match)
        return result

    def scrub(self, text: str | None) -> str | None:
        if text is None:
            return None
        text = _EMAIL.sub(EMAIL_MASK, text)
        text = _PHONE.sub(PHONE_MASK, text)
        text = _CIVILITY.sub(lambda m: f"{m['civ']} {MASK}" + (" " if m[0][-1].isspace() else ""), text)
        if self._pattern is not None:
            parts: list[str] = []
            start = 0
            for match in self._person_matches(text):
                parts.extend((text[start : match.start()], MASK))
                start = match.end()
            text = "".join([*parts, text[start:]])
        return _REPEATED_MASK.sub(MASK, text)

    def leaks(self, text: str) -> list[str]:
        """Person data still present in `text` (used by the privacy audit)."""
        found = [m[0] for m in _EMAIL.finditer(text)] + [m[0] for m in _PHONE.finditer(text)]
        found += [
            m["name"].strip()
            for m in _CIVILITY.finditer(text)
            if MASK not in m["name"] and not set(fold(m["name"]).split()) <= _LABELS  # « Monsieur Nom Prénom(s) »
        ]
        if self._pattern is not None:
            found += [m[0] for m in self._person_matches(text)]
        return found
