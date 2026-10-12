"""Comparable field values of a filing: shared by corrections (human review) and evaluation (field gold)."""

from radar.domain.enums import PowerStatus
from radar.domain.models import Filing
from radar.domain.privacy import MASKED_PERSON

Value = str | float | None


def field_values(filing: Filing) -> dict[str, Value]:
    """Current values (declared powers only; estimates are not facts to compare)."""
    known = filing.power.status is PowerStatus.KNOWN
    return {
        "category": filing.classification.category.value,
        "developer": None if filing.developer == MASKED_PERSON else filing.developer,  # a person is "no company"
        "siret": filing.siret,
        "commune": filing.location.commune,
        "department": filing.location.department,
        "power_mw": filing.power.mw if known else None,
        "power_mw_min": filing.power.mw_min if known else None,
        "energy_mwh": filing.power.mwh,
        "substation": filing.substation,
        "filed_on": filing.filed_on.isoformat() if filing.filed_on else None,
        "review_decision": filing.review_decision.value,
    }


def machine_values(filing: Filing) -> dict[str, Value]:
    """What the machine produced, before human corrections (evaluation always uses this)."""
    return dict(filing.machine.values) if filing.machine else field_values(filing)
