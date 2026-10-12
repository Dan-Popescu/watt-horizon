"""File exports of project summaries: CSV for spreadsheets, GeoJSON for GIS tools. Pure functions."""

import csv
import io
from collections.abc import Iterable
from typing import Any

from radar.api.views import ProjectSummary

QUERY_ONLY = frozenset({"distance_km"})  # meaningful only for a search around a point
COLUMNS: tuple[str, ...] = tuple(name for name in ProjectSummary.model_fields if name not in QUERY_ONLY)


def to_csv(projects: Iterable[ProjectSummary]) -> str:
    """French Excel conventions: `;` separator, decimal comma. Write with encoding `utf-8-sig` (BOM) for Excel."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", lineterminator="\n")
    writer.writeheader()
    for p in projects:
        row = p.model_dump(include=set(COLUMNS))
        writer.writerow({k: f"{v:g}".replace(".", ",") if isinstance(v, float) else v for k, v in row.items()})
    return buffer.getvalue()


def to_geojson(projects: Iterable[ProjectSummary]) -> dict[str, Any]:
    """FeatureCollection (WGS84). Projects without coordinates keep a null geometry, as GeoJSON allows."""
    features = []
    for p in projects:
        props = p.model_dump(exclude={"lat", "lon", *QUERY_ONLY})
        geometry = {"type": "Point", "coordinates": [p.lon, p.lat]} if p.lat is not None and p.lon is not None else None
        features.append({"type": "Feature", "id": p.project_id, "geometry": geometry, "properties": props})
    return {"type": "FeatureCollection", "features": features}
