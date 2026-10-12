"""DREAL case-by-case decision: the « Vu le formulaire … » clause and Article 1.

Typical clause:
  « Vu le formulaire d'examen au cas par cas n° 005382 KKP, déposé complet le 8 septembre 2025, par la société
    DECADE Energy ASSETS relatif au projet de création d'une unité de stockage …, sur la commune de Meru,
    dans le département de l'Oise ; »
The clause is isolated first, then each field is read with a small dedicated pattern (robust to word order).
"""

import contextlib
import re
from datetime import date

from radar.domain.departments import code_from_name
from radar.domain.enums import ReviewDecision
from radar.domain.models import Evidence
from radar.domain.power import read_power
from radar.domain.text import squash
from radar.extraction.facts import Facts
from radar.extraction.substation import find_substation

MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        ),
        start=1,
    )
}
APOS = "[’']"

_CLAUSE = re.compile(r"Vu (?:la demande|le formulaire)[^;]{20,700}?;", re.IGNORECASE)
_NUMBER = re.compile(r"n\s*[°º]\s*([\w/ -]{3,20}?)\s*,")
_DATE = re.compile(r"le (\d{1,2})(?:er)? ([a-zéû]+) (\d{4})", re.IGNORECASE)
# « par » follows « au cas » in « au cas par cas »: the look-behind skips it.
_DEVELOPER = re.compile(
    rf"(?<!cas )\bpar (?:la société |la |le |l{APOS}|Monsieur |Madame )?(.+?)"
    rf"(?:,? le \d|,? relati(?:f|ve)\b|,? (?:située? à [^,]+,\s*)?concernant\b)",
    re.IGNORECASE,
)
_PROJECT = re.compile(
    r"(?:relati(?:f|ve) (?:au|à|a)|concernant) (.+?),? (?:sur (?:la|les) communes?|pour le compte|;|$)", re.IGNORECASE
)
_COMMUNE = re.compile(
    rf"sur (?:la|les) communes? (?:de |d{APOS})(.+?)(?:,|;| dans le| et les informations)", re.IGNORECASE
)
_DEPARTMENT = re.compile(rf"dans le département (?:de l{APOS}|d{APOS}|de |du |des )?([^;,]+)", re.IGNORECASE)
_ARTICLE_1 = re.compile(
    r"Article\s*1(?:er| er)?\s*:?\s*(.{20,400}?(?:n.est pas soumis|est soumis)[^.]{0,120})", re.IGNORECASE
)


def extract_decision(document_id: str, text: str, title: str = "") -> Facts:
    facts = Facts()
    t = squash(text)

    def ev(locator: str, quote: str) -> Evidence:
        return Evidence(document_id=document_id, locator=locator, quote=squash(quote)[:500])

    clause = next((m[0] for m in _CLAUSE.finditer(t) if re.search(r"complet", m[0], re.I)), None)
    if clause:
        quote = ev("clause:request", clause)
        if m := _NUMBER.search(clause):
            facts.set("number", squash(m[1]), quote)
        if (m := _DATE.search(clause)) and m[2].lower() in MONTHS:
            with contextlib.suppress(ValueError):
                facts.set("filed_on", date(int(m[3]), MONTHS[m[2].lower()], int(m[1])), quote)
        if m := _DEVELOPER.search(clause):
            facts.set("developer", squash(m[1]), quote)
        if m := _PROJECT.search(clause):
            facts.set("description", squash(m[1]), quote)
        if m := _COMMUNE.search(clause):
            facts.set("commune", squash(m[1]).replace("- ", "-"), quote)
        if m := _DEPARTMENT.search(clause):
            facts.set("department", code_from_name(m[1]))

    if a := _ARTICLE_1.search(t):
        phrase = squash(a[1])
        decision = (
            ReviewDecision.NOT_SUBJECT if re.search(r"n.est pas soumis", phrase, re.I) else ReviewDecision.SUBJECT
        )
        facts.set("review_decision", decision, ev("article:1", phrase))
    elif title:  # scanned decisions: the official title still states the outcome
        if re.search(r"Décision de non soumission", title, re.I):
            facts.set("review_decision", ReviewDecision.NOT_SUBJECT, ev("title", title))
        elif re.search(r"Décision de soumission", title, re.I):
            facts.set("review_decision", ReviewDecision.SUBJECT, ev("title", title))

    if (
        facts.commune is None
        and title
        and (
            m := re.search(
                rf"(?:communes? (?:de |d{APOS})|site \w+ de )([A-ZÉÈ][\w’'\- ]+?)(?: \((\d{{2}})\)| et |,|$)", title
            )
        )
    ):
        facts.set("commune", m[1].strip(), ev("title", title))
        facts.set("department", m[2])
    if found := find_substation(t):
        facts.set("substation", found[0], ev("text", found[1]))
    # Decisions often restate the power (« 5 MW », « 130 MWac / 260 MWh »); the form's value wins when both exist.
    if reading := read_power(t):
        facts.mw_min, facts.mw_max, facts.mwh = reading.mw_min, reading.mw_max, reading.mwh
        facts.evidence["power"] = ev("text", reading.quote)
    return facts
