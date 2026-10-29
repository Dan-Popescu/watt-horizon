"""File exports of project summaries: CSV for spreadsheets, GeoJSON for GIS tools. Pure functions."""

import csv
import io
import json
from collections.abc import Generator, Iterable, Iterator
from typing import Any

from radar.api.views import ProjectSummary
from radar.domain.models import Project

QUERY_ONLY = frozenset({"distance_km"})  # meaningful only for a search around a point
COLUMNS: tuple[str, ...] = tuple(name for name in ProjectSummary.model_fields if name not in QUERY_ONLY)


def to_csv(projects: Iterable[ProjectSummary]) -> str:
    """French Excel conventions: `;` separator, decimal comma. Write with encoding `utf-8-sig` (BOM) for Excel."""
    return "".join(iter_csv(projects))


def _summary(project: Project | ProjectSummary) -> ProjectSummary:
    if isinstance(project, ProjectSummary):
        return project
    from radar.api.queries import _summary as project_summary

    return project_summary(project)


def _close(rows: Iterator[Project | ProjectSummary]) -> None:
    close = getattr(rows, "close", None)
    if close is not None:
        close()


def iter_csv(projects: Iterable[Project | ProjectSummary]) -> Generator[str, None, None]:
    """Yield a header then one row at a time, without BOM or retaining the input dataset."""
    rows = iter(projects)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", lineterminator="\n")
    try:
        writer.writeheader()
        yield buffer.getvalue()
        for project in rows:
            buffer.seek(0)
            buffer.truncate()
            row = _summary(project).model_dump(include=set(COLUMNS))
            writer.writerow({k: f"{v:g}".replace(".", ",") if isinstance(v, float) else v for k, v in row.items()})
            yield buffer.getvalue()
    finally:
        buffer.close()
        _close(rows)


def _feature(project: ProjectSummary) -> dict[str, Any]:
    props = project.model_dump(exclude={"lat", "lon", *QUERY_ONLY})
    geometry = (
        {"type": "Point", "coordinates": [project.lon, project.lat]}
        if project.lat is not None and project.lon is not None
        else None
    )
    return {"type": "Feature", "id": project.project_id, "geometry": geometry, "properties": props}


def iter_geojson(projects: Iterable[Project | ProjectSummary]) -> Generator[str, None, None]:
    """Yield a valid FeatureCollection incrementally and close an interrupted input cursor."""
    rows = iter(projects)
    try:
        yield '{"type":"FeatureCollection","features":['
        for index, project in enumerate(rows):
            yield ("," if index else "") + json.dumps(_feature(_summary(project)), ensure_ascii=False)
        yield "]}"
    finally:
        _close(rows)


def to_geojson(projects: Iterable[ProjectSummary]) -> dict[str, Any]:
    """FeatureCollection (WGS84). Projects without coordinates keep a null geometry, as GeoJSON allows."""
    return {"type": "FeatureCollection", "features": [_feature(project) for project in projects]}
