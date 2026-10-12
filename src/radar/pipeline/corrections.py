"""Human corrections applied over machine output, and the verified references they produce. Pure functions.

Loop: the machine builds a filing -> `machine` keeps its raw output -> corrections override fields -> every
reviewed filing becomes a verified reference (category label + field values) for `radar eval`.
"""

from collections.abc import Iterable
from datetime import date

from radar.classification.gold import GoldLabel
from radar.domain.enums import Category, Confidence, PowerStatus, ReviewDecision
from radar.domain.fields import Value, field_values
from radar.domain.models import Classification, Correction, Evidence, Filing, Location, MachineOutput, Power
from radar.domain.privacy import PersonScrubber
from radar.evaluation.gold import FieldGold
from radar.validation.lookups import Lookups

VALIDATED = "validated"
CORRECTABLE: tuple[str, ...] = (
    "category",
    "developer",
    "siret",
    "commune",
    "power_mw",
    "power_mw_min",
    "energy_mwh",
    "substation",
    "filed_on",
    "review_decision",
)
POWER_FIELDS = frozenset({"power_mw", "power_mw_min", "energy_mwh"})
CAUSES: dict[str, str] = {  # why the machine was wrong: drives where to improve (quality/errors.py)
    "ocr": "lecture / OCR",
    "layout": "mise en page du document",
    "rule": "règle d'extraction",
    "classification": "classement",
    "merge": "fusion des documents du dossier",
    "source": "document ambigu ou erroné",
    "other": "autre",
}
_NOTES = PersonScrubber()  # reviewers' notes: civility names, e-mails and phones are masked too


def with_machine(filing: Filing) -> Filing:
    """Freeze the machine output on a freshly built filing (before any correction)."""
    return filing.model_copy(
        update={
            "machine": MachineOutput(
                classification=filing.classification,
                values=field_values(filing),
                confidence=filing.confidence.value,
            )
        }
    )


def validate_value(name: str, value: Value) -> Value:
    """Normalise and check a correction value; raises ValueError (shown to the reviewer)."""
    if name == VALIDATED:
        return None
    if name not in CORRECTABLE:
        raise ValueError(f"field must be one of {', '.join((*CORRECTABLE, VALIDATED))}")
    if value is None or value == "":
        if name in {"category", "review_decision"}:
            raise ValueError(f"{name} cannot be empty")
        return None
    if name in POWER_FIELDS:
        number = float(str(value).replace(",", "."))
        if not 0 < number < 100_000:
            raise ValueError(f"{name}: implausible value {number}")
        return number
    text = str(value).strip()
    if name == "category":
        category = Category(text)
        if category in {Category.UNCLASSIFIED, Category.NEEDS_REVIEW}:
            raise ValueError("category: choose a real category")
        return category.value
    if name == "review_decision":
        return ReviewDecision(text).value
    if name == "filed_on":
        return date.fromisoformat(text).isoformat()
    if name == "siret":
        digits = "".join(c for c in text if c.isdigit())
        if len(digits) != 14:
            raise ValueError("siret: 14 digits expected")
        return digits
    return text


def latest(corrections: Iterable[Correction]) -> dict[str, Correction]:
    out: dict[str, Correction] = {}
    for c in sorted(corrections, key=lambda c: (c.created_at, c.id or 0)):
        out[c.field] = c
    return out


def apply_corrections(filing: Filing, corrections: Iterable[Correction], lookups: Lookups | None = None) -> Filing:
    by_field = latest(corrections)
    if not by_field:
        return filing
    base = filing if filing.machine else with_machine(filing)
    update: dict[str, object] = {"reviewed_at": max(c.created_at for c in by_field.values())}
    evidence = dict(base.evidence)

    def human(c: Correction) -> Evidence:
        note = _NOTES.scrub(c.note) or ""
        return Evidence(document_id="human", locator=f"correction:{c.id}", quote=note or "Vérifié par un relecteur")

    for name, c in by_field.items():
        if name in (VALIDATED, *POWER_FIELDS):
            continue
        if name == "category":
            update["classification"] = Classification(
                category=Category(str(c.value)), method="human", confidence="reviewed", quote=_NOTES.scrub(c.note) or ""
            )
        elif name == "review_decision":
            update["review_decision"] = ReviewDecision(str(c.value))
        elif name == "filed_on":
            update["filed_on"] = date.fromisoformat(str(c.value)) if c.value else None
        elif name == "commune":
            update["location"] = _relocate(base.location, str(c.value) if c.value else None, lookups)
        else:  # developer, siret, substation
            update[name] = c.value
        evidence[name] = human(c)

    if power := {n: by_field[n] for n in POWER_FIELDS if n in by_field}:
        update["power"] = _corrected_power(base.power, power)
        evidence["power"] = human(max(power.values(), key=lambda c: c.created_at))

    if VALIDATED in by_field:
        update["confidence"] = Confidence.HIGH
    return base.model_copy(update={**update, "evidence": evidence})


def _corrected_power(current: Power, corrections: dict[str, Correction]) -> Power:
    known = current.status is PowerStatus.KNOWN
    values: dict[str, float | None] = {
        "power_mw": current.mw_max if known else None,
        "power_mw_min": current.mw_min if known else None,
        "energy_mwh": current.mwh,
    }
    values.update({name: float(c.value) if c.value is not None else None for name, c in corrections.items()})
    mw_max = values["power_mw"]
    mw_min = values["power_mw_min"] if values["power_mw_min"] is not None else mw_max
    status = PowerStatus.KNOWN if mw_max is not None else PowerStatus.UNKNOWN
    return Power(mw_min=mw_min, mw_max=mw_max, mwh=values["energy_mwh"], status=status, basis="correction")


def _relocate(current: Location, commune: str | None, lookups: Lookups | None) -> Location:
    if commune is None:
        return Location(department=current.department)
    # Same department first; then nationwide (the reviewer may move the project), unique names only.
    info = (lookups.commune(commune, current.department) or lookups.commune(commune, None)) if lookups else None
    if info is None:
        return Location(commune=commune, department=current.department)  # unknown commune: no position
    return Location(
        commune=info.name,
        department=info.department,
        insee_code=info.insee_code,
        lat=info.lat,
        lon=info.lon,
        precision="commune" if info.lat is not None else "none",
    )


def reviewed_references(
    filings: Iterable[Filing], corrections: Iterable[Correction]
) -> tuple[dict[str, GoldLabel], dict[str, FieldGold]]:
    """Verified references from human review: a validated filing confirms every current value; a corrected
    field confirms that field. The category label comes from the corrected (or validated) filing."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    labels: dict[str, GoldLabel] = {}
    fields: dict[str, FieldGold] = {}
    for filing in filings:
        latest_by_field = latest(by_key.get(filing.key, []))
        if not latest_by_field:
            continue
        reviewer = max(latest_by_field.values(), key=lambda c: c.created_at).author
        values = field_values(filing)
        checked = set(values) if VALIDATED in latest_by_field else set(latest_by_field) - {VALIDATED}
        if "category" in checked:
            labels[filing.number] = GoldLabel(
                filing.number, filing.classification.category, "", f"review:{reviewer}", verified=True
            )
        expected = {k: v for k, v in values.items() if k in checked and k != "category"}
        if expected:
            fields[filing.number] = FieldGold(filing.number, expected, f"review:{reviewer}", verified=True)
    return labels, fields
