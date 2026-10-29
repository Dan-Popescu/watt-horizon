"""MCP server (read-only) exposing the project database to agents.

Run over stdio:  uv run radar mcp            (RADAR_DATA_DIR selects the database)
Inspect:         uv run mcp dev src/radar/mcp_server.py

Each tool opens its own short-lived database connection (SQLite connections are not shared across threads).
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from radar.api import queries
from radar.api.views import ChangeView, DatasetInfo, ProjectDetail, ProjectPage, ProjectSummary, ZoneSummary
from radar.config import load_settings
from radar.store.db import Database

T = TypeVar("T")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Database of French grid-connection projects (battery storage, data centers) detected early in public filings.
Start with `dataset_info` to know coverage and caveats. Locate projects with `search_projects` or `zone_pressure`,
then call `get_project` for sources, quotes and checks. `what_changed` lists what is new since a date. Always report `power_status` and `confidence` to the user:
'estimated' powers are ranges, 'review' projects are not yet human-checked. Categories: bess, bess_hybrid,
storage_substation, datacenter. Departments are codes like "59", "2A"."""


def build_server(db_path: Path | str | Callable[[], Path]) -> MCPServer:
    mcp = MCPServer("watt-horizon", instructions=INSTRUCTIONS, version="0.1.0")

    def run(fn: Callable[[Database], T]) -> T:
        try:
            selected_path = Path(db_path() if callable(db_path) else db_path)
            if not selected_path.exists():
                raise ToolError("No database available. Run radar run before serving data.")
            with Database(selected_path, read_only=True) as db:
                return fn(db)
        except ValueError as exc:  # invalid category, etc.
            raise ToolError(str(exc)) from exc
        except (sqlite3.Error, OSError) as exc:
            raise ToolError("Dataset temporarily unavailable") from exc

    @mcp.tool(annotations=READ_ONLY)
    def dataset_info() -> DatasetInfo:
        """Coverage, freshness and caveats of the dataset. Call first."""
        return run(queries.dataset_info)

    @mcp.tool(annotations=READ_ONLY)
    def search_projects(
        category: str | None = None,
        department: str | None = None,
        commune: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float = 20.0,
        min_mw: float | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[ProjectSummary]:
        """Find projects. Filters combine. With lat/lon, results are within radius_km and sorted by distance.
        `since` is an ISO date (first_seen >= since). `category`: bess | bess_hybrid | storage_substation | datacenter."""
        return run(
            lambda db: queries.search_projects(
                db,
                category=category,
                department=department,
                commune=commune,
                lat=lat,
                lon=lon,
                radius_km=radius_km,
                min_mw=min_mw,
                since=since,
                limit=limit,
            )
        )

    @mcp.tool(annotations=READ_ONLY)
    def project_page(
        category: str | None = None,
        department: str | None = None,
        commune: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float = 20.0,
        min_mw: float | None = None,
        since: str | None = None,
        query: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ProjectPage:
        """Page through all matching projects using next_cursor with the same filters. Totals cover every match."""
        return run(
            lambda db: queries.project_page(
                db,
                category=category,
                department=department,
                commune=commune,
                lat=lat,
                lon=lon,
                radius_km=radius_km,
                min_mw=min_mw,
                since=since,
                query=query,
                limit=limit,
                cursor=cursor,
            )
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_project(project_id: str) -> ProjectDetail:
        """Full record: source documents (URLs), quoted evidence per field, automatic checks, related signals."""
        detail = run(lambda db: queries.get_project(db, project_id))
        if detail is None:
            raise ToolError(f"Unknown project_id: {project_id}")
        return detail

    @mcp.tool(annotations=READ_ONLY)
    def zone_pressure(lat: float, lon: float, radius_km: float = 15.0) -> ZoneSummary:
        """How contested is the grid around a point: projects within radius_km, total known power, by category."""
        return run(lambda db: queries.zone_pressure(db, lat, lon, radius_km))

    @mcp.tool(annotations=READ_ONLY)
    def projects_near_substation(substation: str, limit: int = 50) -> list[ProjectSummary]:
        """Projects whose filings name this substation (e.g. "Margot", "Les Hoquins", "400kV")."""
        return run(lambda db: queries.projects_near_substation(db, substation, limit))

    @mcp.tool(annotations=READ_ONLY)
    def what_changed(
        since: str, category: str | None = None, department: str | None = None, limit: int = 100
    ) -> list[ChangeView]:
        """Change log since an ISO date (e.g. "2026-10-01"), newest first: new projects, changed fields
        (power, stage, substation…) with before/after values, and projects no longer tracked. Use it for alerts."""
        return run(lambda db: queries.what_changed(db, since, category=category, department=department, limit=limit))

    return mcp


def main() -> None:
    build_server(load_settings().db_path).run(transport="stdio")


if __name__ == "__main__":
    main()
