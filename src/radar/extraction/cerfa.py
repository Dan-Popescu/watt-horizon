"""Cerfa 14734 (case-by-case review request): form fields first, text layer as fallback.

Three layouts seen in the wild:
- 2025+ "camelCase" fields (denomination, numeroSiret, Valeurs, lat1Forprojectaddress…)
- 2025+ "numbered" fields ("2.2. Denomination", "4.6 Valeurs", split coordinates…)
- older flattened PDFs (no fields): values appear inline in the text layer.
"""

import contextlib
import re
from datetime import date

from radar.domain.geo import in_france, parse_dms, parse_dms_parts
from radar.domain.ids import find_siret
from radar.domain.models import Evidence
from radar.domain.power import read_area_ha, read_containers, read_power
from radar.domain.text import excerpt, squash
from radar.extraction.facts import Facts
from radar.extraction.substation import find_substation
from radar.reading.pdf import PdfContent

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("IntituleDuProjetValeur", "Intitulé du projet"),
    "developer": ("denomination", "2.2. Denomination"),
    "legal_name": ("raisonsociale", "2.2. Raison sociale"),
    "siret": ("numeroSiret", "2.2. SIRET"),
    "filed": ("Dossiercompletdate",),
    "number": ("referenceNumberValue",),
    "description": ("NatureDuProjetY", "4.1 Nature du projet"),
    "objectives": ("ObjectifsDuProjetValeur", "4.2 Objectifs du projet"),
    "procedures": ("AQuelleSProcSoumis1", "4.4 Procedures administratives"),
    "quantities": ("Valeurs", "4.6 Valeurs"),
    "quantity_labels": ("Grandeurscaracteristiquesduprojet", "4.6 Grandeurs du projet"),
    "locality": ("LocalityNameShared", "4.6 Localite"),
    "postcode": ("codePostalShared", "4.6 Code postal"),
    "lat": ("lat1Forprojectaddress",),
    "lon": ("long1ForprojectAddress",),
}
# Legal forms are not company names ("Societe par actions simplifiee" is sometimes typed as the legal name).
_LEGAL_FORMS = re.compile(r"(?i)^(soci[ée]t[ée]s?( par actions simplifi[ée]e)?|sas|sarl|sa|sasu)$")
_POSTCODE_PREFIX = re.compile(r"^.*?\b(\d{5})\s+")


def _pick(fields: dict[str, str], key: str) -> tuple[str, str] | None:
    for name in FIELD_ALIASES[key]:
        if fields.get(name):
            return name, fields[name]
    return None


def _date_ddmmyyyy(raw: str) -> date | None:
    m = re.search(r"(\d{2})[-/ ](\d{2})[-/ ](\d{4})", raw)
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None


def _commune_from_locality(raw: str) -> tuple[str, str | None]:
    """'4 Allee Lucien Barbier 60110 Meru' -> ('Meru', '60110'); 'Aubenton' -> ('Aubenton', None)."""
    m = _POSTCODE_PREFIX.match(raw)
    if m:
        return squash(raw[m.end() :]), m[1]
    return squash(raw), None


def extract_cerfa(document_id: str, content: PdfContent) -> Facts:
    facts = _from_fields(document_id, content.fields) if content.fields else Facts()
    text = content.text
    _complete_from_text(document_id, text, facts)
    return facts


def _from_fields(doc: str, f: dict[str, str]) -> Facts:
    facts = Facts()

    def ev(name: str, value: str) -> Evidence:
        return Evidence(document_id=doc, locator=f"field:{name}", quote=squash(value)[:300])

    for key, target in (
        ("title", "title"),
        ("description", "description"),
        ("procedures", "procedures"),
        ("number", "number"),
        ("developer", "developer"),
    ):
        if hit := _pick(f, key):
            facts.set(target, squash(hit[1]), ev(*hit))
    if (hit := _pick(f, "legal_name")) and not _LEGAL_FORMS.match(squash(hit[1])):
        facts.set("legal_name", squash(hit[1]), ev(*hit))
    if hit := _pick(f, "siret"):
        digits = re.sub(r"\D", "", hit[1])
        facts.set("siret", digits if len(digits) == 14 else None, ev(*hit))
    if hit := _pick(f, "filed"):
        facts.set("filed_on", _date_ddmmyyyy(hit[1]), ev(*hit))
    if hit := _pick(f, "locality"):
        commune, postcode = _commune_from_locality(hit[1])
        facts.set("commune", commune, ev(*hit))
        facts.set("postcode", postcode)
    if hit := _pick(f, "postcode"):
        facts.set("postcode", re.sub(r"\D", "", hit[1])[:5] or None, ev(*hit))

    lat = parse_dms(f.get("lat1Forprojectaddress")) or parse_dms_parts(
        f.get("4.6 Coordonnees_Lat"),
        f.get("4.6 Coordonnees_Lat °"),
        f.get("4.6 Coordonnees_Lat '"),
        f.get('4.6 Coordonnees_Lat "'),
    )
    lon = parse_dms(f.get("long1ForprojectAddress")) or parse_dms_parts(
        f.get("4.6 Coordonnees_long"),
        f.get("4.6 Coordonnees_long °"),
        f.get("4.6 Coordonnees_long '"),
        f.get('4.6 Coordonnees_long "'),
    )
    if in_france(lat, lon):
        coords = f"{f.get('lat1Forprojectaddress') or f.get('4.6 Coordonnees_Lat')} / {f.get('long1ForprojectAddress') or f.get('4.6 Coordonnees_long')}"
        facts.set(
            "lat",
            round(lat, 5) if lat is not None else None,
            Evidence(document_id=doc, locator="field:coordinates", quote=coords),
        )
        facts.set("lon", round(lon, 5) if lon is not None else None)

    for key in ("quantities", "description", "objectives"):
        hit = _pick(f, key)
        if hit and (reading := read_power(hit[1])):
            evidence = Evidence(document_id=doc, locator=f"field:{hit[0]}", quote=reading.quote)
            _set_power(facts, reading.mw_min, reading.mw_max, reading.mwh, evidence)
            break
    prose = _joined(f, "description", "objectives")
    facts.set("containers", read_containers(prose))
    facts.set("area_ha", read_area_ha(_joined(f, "quantities") or prose))
    if found := find_substation(prose):
        facts.set("substation", found[0], Evidence(document_id=doc, locator="field:description", quote=found[1]))
    return facts


def _joined(fields: dict[str, str], *keys: str) -> str:
    return " ".join(hit[1] for key in keys if (hit := _pick(fields, key)))


def _set_power(facts: Facts, mw_min: float | None, mw_max: float | None, mwh: float | None, evidence: Evidence) -> None:
    facts.mw_min, facts.mw_max, facts.mwh = mw_min, mw_max, mwh
    facts.evidence["power"] = evidence


def _complete_from_text(doc: str, text: str, facts: Facts) -> None:
    """Fill what form fields did not give (older flattened forms, or values only stated in prose)."""
    if not text:
        return
    if facts.siret is None and (m := find_siret(text)):
        facts.set("siret", m[1], Evidence(document_id=doc, locator="text", quote=excerpt(text, m.start(1), m.end(1))))
    if facts.filed_on is None and (m := re.search(r"\b(\d{2}) (\d{2}) (20\d{2}) (20\d{2})[_-](\d{3,4})\b", text)):
        with contextlib.suppress(ValueError):
            facts.set(
                "filed_on", date(int(m[3]), int(m[2]), int(m[1])), Evidence(document_id=doc, locator="text", quote=m[0])
            )
    description = re.search(r"Le projet consiste[^.]{0,400}\.(?:[^.]{0,300}\.)?", text)
    if facts.description is None and description:
        facts.set(
            "description",
            squash(description[0]),
            Evidence(document_id=doc, locator="text", quote=squash(description[0])[:300]),
        )
    if facts.mw_max is None and facts.mwh is None:
        reading = read_power(facts.description) or read_power(text)
        if reading:
            _set_power(
                facts,
                reading.mw_min,
                reading.mw_max,
                reading.mwh,
                Evidence(document_id=doc, locator="text", quote=reading.quote),
            )
    if facts.commune is None and (
        m := re.search(r"sur la commune d(?:e |[’'])([A-ZÉÈ][\w’'\- ]+?)\s*\((\d{2}|2[AB])\)", text)
    ):
        facts.set(
            "commune", m[1].strip(), Evidence(document_id=doc, locator="text", quote=excerpt(text, m.start(), m.end()))
        )
        facts.set("department", m[2])
    facts.set("containers", facts.containers or read_containers(text))
    if facts.substation is None and (found := find_substation(facts.description, text)):
        facts.set("substation", found[0], Evidence(document_id=doc, locator="text", quote=found[1]))
