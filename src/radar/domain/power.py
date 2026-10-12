"""Power reading from text, and estimation from physical clues when no document states it.

Estimation constants are calibrated on projects whose power AND clues are both known. Keep `CALIBRATION`
up to date: every newly known project should be added, then the constants recomputed.
"""

import re
from dataclasses import dataclass

from radar.domain.enums import PowerStatus
from radar.domain.models import Power
from radar.domain.text import excerpt, parse_number

_NUM = r"(\d{1,4}(?:[   ]\d{3})*(?:[.,]\d+)?)"
_MW_UNIT = r"MW(?:ac|c|e)?\b(?!h)"
_RANGE = re.compile(rf"{_NUM}\s*(?:,\s*)?(?:{_MW_UNIT})?\s*(?:-|à|a)\s*{_NUM}\s*,?\s*{_MW_UNIT}", re.IGNORECASE)
_SINGLE = re.compile(rf"{_NUM}\s*,?\s*{_MW_UNIT}", re.IGNORECASE)
_MWH = re.compile(rf"{_NUM}\s*,?\s*MWh\b", re.IGNORECASE)
_CONTAINERS = re.compile(r"(\d{1,3})\s+(?:conteneurs|containers)\b", re.IGNORECASE)
_AREA_HA = re.compile(rf"\(?\s*{_NUM}\s*,?\s*(?:ha|hectares?)\b", re.IGNORECASE)

# Plausibility bounds for a single grid-scale site.
MAX_MW = 3000.0
MAX_DURATION_H = 8.0
MIN_DURATION_H = 0.5


@dataclass(frozen=True)
class PowerReading:
    mw_min: float | None
    mw_max: float | None
    mwh: float | None
    quote: str


def read_power(text: str | None) -> PowerReading | None:
    """First power statement in `text`: '47 MW / 94 MWh', '(10-20, MW)', '50 MW à 80 MW', '130 MWac'."""
    if not text:
        return None
    rng, one, mwh = _RANGE.search(text), _SINGLE.search(text), _MWH.search(text)
    mw_min = mw_max = None
    anchor = None
    if rng and (one is None or rng.start() <= one.start()):
        mw_min, mw_max, anchor = parse_number(rng.group(1)), parse_number(rng.group(2)), rng
    elif one:
        mw_min = mw_max = parse_number(one.group(1))
        anchor = one
    mwh_value = parse_number(mwh.group(1)) if mwh else None
    anchor = anchor or mwh
    if anchor is None or (mw_max is not None and mw_max > MAX_MW):
        return None
    return PowerReading(mw_min, mw_max, mwh_value, excerpt(text, anchor.start(), anchor.end()))


def read_containers(text: str | None) -> int | None:
    m = _CONTAINERS.search(text or "")
    return int(m.group(1)) if m else None


def read_area_ha(text: str | None) -> float | None:
    m = _AREA_HA.search(text or "")
    return parse_number(m.group(1)) if m else None


# (name, containers, MWh, MW, area_ha) — from DREAL Hauts-de-France forms read on 2026-10-12.
CALIBRATION: tuple[tuple[str, int, float, float, float | None], ...] = (
    ("Aubenton", 36, 200.0, 100.0, 1.2),
    ("Bonneuil-les-Eaux", 16, 94.0, 47.0, 0.85),
    ("Nibas", 20, 90.0, 45.0, None),
)


def _ratios() -> tuple[float, float, float, float]:
    per_container = [mwh / n for _, n, mwh, _, _ in CALIBRATION]
    per_ha = [mw / ha for _, _, _, mw, ha in CALIBRATION if ha]
    return min(per_container), max(per_container), min(per_ha), max(per_ha)


def estimate_power(containers: int | None, area_ha: float | None, duration_h: float = 2.0) -> Power | None:
    """Range estimate from container count (preferred) or fenced area. Always flagged ESTIMATED."""
    lo_c, hi_c, lo_ha, hi_ha = _ratios()
    if containers:
        mwh_lo, mwh_hi = containers * lo_c, containers * hi_c
        return Power(
            mw_min=round(mwh_lo / duration_h),
            mw_max=round(mwh_hi / duration_h),
            mwh=round((mwh_lo + mwh_hi) / 2),
            status=PowerStatus.ESTIMATED,
            basis=f"estimation : {containers} conteneurs x {lo_c:.1f}-{hi_c:.1f} MWh, durée {duration_h:g} h",
        )
    if area_ha:
        return Power(
            mw_min=round(area_ha * lo_ha),
            mw_max=round(area_ha * hi_ha),
            status=PowerStatus.ESTIMATED,
            basis=f"estimation : {area_ha:g} ha x {lo_ha:.0f}-{hi_ha:.0f} MW/ha",
        )
    return None


def storage_duration_ok(mw: float | None, mwh: float | None) -> bool | None:
    """None if not computable; otherwise whether MWh/MW is a plausible battery duration."""
    if not mw or not mwh:
        return None
    return MIN_DURATION_H <= mwh / mw <= MAX_DURATION_H
