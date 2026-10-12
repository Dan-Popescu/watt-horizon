"""Parse a value captured on `fold_keep` text (rule suggestions and adopted rules). No other dependency."""

import re

from radar.quality.locate import MONTHS_FR


def parse(field_name: str, raw: str) -> str | float | None:
    raw = raw.strip(" -'")
    if field_name in {"power_mw", "power_mw_min", "energy_mwh"}:
        try:
            return float(raw.replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    if field_name == "siret":
        return raw.replace(" ", "")
    if field_name == "filed_on":
        m = re.match(r"(\d{1,2})(?:er)? (\w+) (\d{4})", raw)
        if m and m[2] in MONTHS_FR:
            return f"{m[3]}-{MONTHS_FR.index(m[2]) + 1:02d}-{int(m[1]):02d}"
        d = re.split(r"[ /.-]", raw)
        return f"{d[2]}-{d[1]}-{d[0]}" if len(d) == 3 and all(x.isdigit() for x in d) else None
    return raw
