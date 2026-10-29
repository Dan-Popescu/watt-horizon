"""Strict match between a tracked filing and an installation in service (national register).

A match requires all of: same INSEE commune; commissioned on or after the filing date; installed power within
±30 % of the declared power (or the filing's power is unknown, in which case the installation must be ≥ 1 MW).
Small older installations in the same commune (frequent) therefore never mark a project as commissioned.
"""

from collections.abc import Iterable

from radar.domain.enums import SourceKind
from radar.domain.models import Filing, Signal

POWER_TOLERANCE = 0.3
MIN_UNKNOWN_POWER_MW = 1.0


def compatible(filing: Filing, installation: Signal) -> bool:
    insee = installation.payload.get("insee")
    if not insee or insee != filing.location.insee_code:
        return False
    if filing.filed_on and installation.observed_on and installation.observed_on < filing.filed_on:
        return False
    installed = float(installation.payload.get("power_mw", "0"))
    declared = filing.power.mw
    if declared is None:
        return installed >= MIN_UNKNOWN_POWER_MW
    return abs(installed - declared) <= POWER_TOLERANCE * declared


def commissioned(filings: Iterable[Filing], signals: Iterable[Signal]) -> dict[str, Signal]:
    """filing key -> matching installation in service (the closest in power when several match)."""
    installations: dict[str, list[Signal]] = {}
    for signal in signals:
        if signal.source is SourceKind.ENERGY_REGISTRY and (insee := signal.payload.get("insee")):
            installations.setdefault(insee, []).append(signal)
    matches: dict[str, Signal] = {}
    for filing in filings:
        candidates = [s for s in installations.get(filing.location.insee_code or "", ()) if compatible(filing, s)]
        if candidates:
            declared = filing.power.mw or 0.0
            matches[filing.key] = min(candidates, key=lambda s: abs(float(s.payload["power_mw"]) - declared))
    return matches
