"""Coordinates: parsing the formats found in French forms, and distances."""

import math
import re

# Metropolitan France + Corsica, generous margins. Overseas departments are out of scope for now.
FRANCE_BBOX = (41.0, -5.5, 51.5, 10.0)  # lat_min, lon_min, lat_max, lon_max

_DMS = re.compile(
    r"""^\s*(?P<deg>-?\d+(?:\.\d+)?)\s*°\s*
        (?:(?P<min>\d+(?:\.\d+)?)\s*['’]\s*)?
        (?:(?P<sec>\d+(?:\.\d+)?)\s*(?:"|''|”)?\s*(?P<frac>\d+)?\s*)?
        (?P<hemi>[NSEWO])?\s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def parse_dms(raw: str | None) -> float | None:
    """Parse '49°50'58"6', '49°13'11.8416"N', '2°14'54"2' (forms write tenths after the seconds mark)."""
    if not raw:
        return None
    m = _DMS.match(raw.replace(",", "."))
    if not m:
        return None
    seconds = float(m["sec"] or 0)
    if m["frac"] and m["sec"] and "." not in m["sec"]:
        seconds = float(f"{m['sec']}.{m['frac']}")
    value = float(m["deg"]) + float(m["min"] or 0) / 60 + seconds / 3600
    return -value if (m["hemi"] or "").upper() in {"S", "W", "O"} else value


def parse_dms_parts(deg: str | None, minutes: str | None, seconds: str | None, hemi: str | None) -> float | None:
    """Newer forms split coordinates into four fields."""
    if not deg:
        return None
    try:
        value = float(deg) + float(minutes or 0) / 60 + float((seconds or "0").replace(",", ".")) / 3600
    except ValueError:
        return None
    return -value if (hemi or "").strip().upper() in {"S", "W", "O"} else value


def in_france(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    lat_min, lon_min, lat_max, lon_max = FRANCE_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bbox_around(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Bounding box enclosing a circle: cheap SQL pre-filter before the exact haversine distance."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon
