"""Checks run on every filing before publication. Each returns a `Check`; alerts send the filing to human review."""

import re
from datetime import date

from radar.domain.enums import CheckStatus
from radar.domain.ids import luhn_ok
from radar.domain.models import Check
from radar.domain.power import storage_duration_ok
from radar.domain.privacy import MASKED_PERSON, looks_like_person
from radar.domain.text import fold
from radar.extraction.facts import Facts
from radar.validation.lookups import CommuneInfo, Lookups


def check_natural_person(facts: Facts) -> Check:
    """GDPR minimisation: a developer that is a natural person is masked before anything is stored or published."""
    if facts.developer == MASKED_PERSON or looks_like_person(facts.developer):
        facts.developer = MASKED_PERSON
        facts.evidence.pop("developer", None)
        return Check(name="gdpr", status=CheckStatus.ALERT, detail="porteur = personne physique : nom masqué")
    return Check(name="gdpr", status=CheckStatus.OK, detail="personne morale")


def check_siret(facts: Facts, lookups: Lookups) -> Check:
    s = facts.siret
    if not s:
        return Check(name="siret", status=CheckStatus.UNKNOWN, detail="absent des documents")
    if len(s) != 14 or not s.isdigit() or not luhn_ok(s):
        return Check(name="siret", status=CheckStatus.ALERT, detail=f"format invalide : {s}")
    company = lookups.company(s[:9])
    if company is None:
        return Check(name="siret", status=CheckStatus.UNKNOWN, detail="SIREN non vérifié (hors cache ou introuvable)")
    official = fold(company.name)
    candidates = [fold(facts.developer), fold(facts.legal_name)]  # brand vs project company: accept either
    matched = any(c and _same_company(c, official) for c in candidates)
    detail = f"SIREN {s[:9]} = {company.name}"
    if matched:
        return Check(name="siret", status=CheckStatus.OK, detail=detail)
    return Check(name="siret", status=CheckStatus.ALERT, detail=detail + " (nom différent du porteur)")


def _same_company(declared: str, official: str) -> bool:
    """Folded names; spacing differs between forms and the registry (« AFR59 » vs « AFR 59 »)."""
    compact_declared, compact_official = declared.replace(" ", ""), official.replace(" ", "")
    return (
        compact_declared in compact_official
        or compact_official in compact_declared
        or official.split()[0] in declared.split()
    )


def check_commune(facts: Facts, lookups: Lookups) -> tuple[Check, CommuneInfo | None]:
    if not facts.commune:
        return Check(name="commune", status=CheckStatus.UNKNOWN, detail="absente"), None
    first = re.split(r"\s+et\s+|,", facts.commune)[0].strip()  # « Loon-Plage et Gravelines » -> first commune
    info = lookups.commune(first, facts.department)
    if info is None:
        return Check(name="commune", status=CheckStatus.UNKNOWN, detail=f"« {first} » non vérifiée"), None
    return Check(name="commune", status=CheckStatus.OK, detail=f"{info.name} ({info.insee_code})"), info


def check_gps(facts: Facts, commune: CommuneInfo | None, lookups: Lookups) -> Check:
    if facts.lat is None or facts.lon is None:
        return Check(name="gps", status=CheckStatus.UNKNOWN, detail="pas de coordonnées déclarées")
    found = lookups.commune_at(facts.lat, facts.lon)
    if found is None:
        return Check(name="gps", status=CheckStatus.UNKNOWN, detail="point non vérifié")
    if commune is not None and found.insee_code != commune.insee_code:
        return Check(
            name="gps",
            status=CheckStatus.ALERT,
            detail=f"le point déclaré tombe à {found.name} ({found.insee_code}), pas à {commune.name}",
        )
    return Check(name="gps", status=CheckStatus.OK, detail=f"point situé à {found.name}")


def check_power(facts: Facts) -> Check:
    ok = storage_duration_ok(facts.mw_max, facts.mwh)
    if facts.mw_max is None and facts.mwh is None:
        return Check(name="power", status=CheckStatus.UNKNOWN, detail="non indiquée")
    if ok is False:
        return Check(
            name="power",
            status=CheckStatus.ALERT,
            detail=f"durée invraisemblable : {facts.mwh} MWh / {facts.mw_max} MW",
        )
    return Check(name="power", status=CheckStatus.OK, detail=f"{facts.mw_max or '?'} MW / {facts.mwh or '?'} MWh")


def check_dates(facts: Facts, today: date | None = None) -> Check:
    today = today or date.today()
    if facts.filed_on is None:
        return Check(name="dates", status=CheckStatus.UNKNOWN, detail="date de dépôt absente")
    if not date(2010, 1, 1) <= facts.filed_on <= today:
        return Check(name="dates", status=CheckStatus.ALERT, detail=f"date de dépôt invraisemblable : {facts.filed_on}")
    return Check(name="dates", status=CheckStatus.OK, detail=facts.filed_on.isoformat())
