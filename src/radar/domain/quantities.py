"""Physical measurements with units retained, shared by extraction and OCR evaluation."""

import re
from dataclasses import dataclass
from typing import Literal

from radar.domain.text import parse_number

NUMBER = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?"
UNIT = r"[kMG]?W(?:h)?(?:ac|dc|c)?"
_MEASUREMENT = re.compile(
    rf"(?<![\w.,-])(?P<low>{NUMBER})(?:\s*(?:à|a|et|[-–])\s*(?P<high>{NUMBER}))?\s*(?P<unit>{UNIT})(?![\w])",
    re.IGNORECASE,
)
_RAW = re.compile(rf"(?P<number>{NUMBER})\s*(?P<unit>{UNIT})?", re.IGNORECASE)


@dataclass(frozen=True)
class Measurement:
    low: float
    high: float
    dimension: Literal["power", "energy"]
    basis: str  # empty, ac, dc, c; never silently equate DC to AC
    start: int
    end: int


def _unit(unit: str) -> tuple[Literal["power", "energy"], float, str]:
    milli = unit.startswith("m")  # SI case matters: mW is never MW.
    unit = unit.lower()
    prefix = unit[0] if unit[0] in {"k", "m", "g"} else ""
    factor = 1e-9 if milli else {"k": 0.001, "m": 1.0, "g": 1000.0, "": 0.000001}[prefix]
    dimension: Literal["power", "energy"] = "energy" if "wh" in unit else "power"
    basis = next((suffix for suffix in ("ac", "dc", "c") if unit.endswith(suffix)), "")
    return dimension, factor, basis


def measurements(text: str) -> list[Measurement]:
    """Numbers explicitly attached to units, normalized to MW or MWh; retain each association."""
    out = []
    for match in _MEASUREMENT.finditer(text):
        dimension, factor, basis = _unit(match["unit"])
        low = parse_number(match["low"]) * factor
        high = parse_number(match["high"] or match["low"]) * factor
        if high < low:
            continue
        out.append(Measurement(low, high, dimension, basis, match.start(), match.end()))
    return out


def normalized_value(raw: str, dimension: Literal["power", "energy"]) -> float | None:
    """Parse one number, optionally with a compatible unit. Ranges and negative values are not scalar answers."""
    match = _RAW.fullmatch(raw.strip())
    if match is None:
        return None
    value = parse_number(match["number"])
    if match["unit"]:
        actual_dimension, factor, _ = _unit(match["unit"])
        if actual_dimension != dimension:
            return None
        value *= factor
    return value if 0 < value < 100_000 else None
