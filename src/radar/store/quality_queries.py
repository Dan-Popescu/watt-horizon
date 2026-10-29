"""Read-only aggregates for documentary diagnostics; never expose reviewer identities or source snippets."""

import json
import sqlite3
from typing import Any


def listing_counts(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = connection.execute(
        "SELECT source,COUNT(*),SUM(kept),COUNT(*) FILTER (WHERE kept=0 AND audit='random' AND audit_policy=2),"
        "COUNT(*) FILTER (WHERE kept=0 AND audit LIKE 'near-miss:%'),"
        "COUNT(*) FILTER (WHERE kept=0 AND audit_policy<>2) "
        "FROM listing_items GROUP BY source"
    )
    return {
        row[0]: {
            "seen": row[1],
            "kept": row[2],
            "rejected": row[1] - row[2],
            "random_selected": row[3],
            "near_miss_selected": row[4],
            "legacy_rejected": row[5],
        }
        for row in rows
    }


def random_audit_items(connection: sqlite3.Connection) -> list[tuple[str, tuple[str, ...]]]:
    """Each observation is one rejected listing URL, not one attached PDF."""
    rows = connection.execute(
        "SELECT l.source,COALESCE(json_group_array(s.filing_key) FILTER (WHERE s.filing_key IS NOT NULL),'[]') "
        "FROM listing_items l LEFT JOIN dossier_scans s ON s.root_url=l.url "
        "WHERE l.kept=0 AND l.audit='random' AND l.audit_policy=2 GROUP BY l.source,l.url"
    )
    return [(row[0], tuple(json.loads(row[1]))) for row in rows]


def coverage_scopes(connection: sqlite3.Connection, region: str | None) -> list[tuple[str, dict[str, Any]]]:
    where, args = (" WHERE region=?", (region,)) if region else ("", ())
    return [
        (row[0], json.loads(row[1]))
        for row in connection.execute("SELECT source,data FROM collection_coverage" + where, args)
    ]


def dossier_counts(connection: sqlite3.Connection, region: str | None) -> dict[str, dict[str, int]]:
    where, args = (
        (" WHERE substr(filing_key,instr(filing_key,':')+1,?)=?", (len(region) + 1, region + ":"))
        if region
        else ("", ())
    )
    source = "substr(filing_key,1,instr(filing_key,':')-1)"
    result: dict[str, dict[str, int]] = {}
    for row in connection.execute(
        f"SELECT {source},COALESCE(json_extract(data,'$.state'),'unknown'),COUNT(*) "
        "FROM dossier_scans" + where + " GROUP BY 1,2",
        args,
    ):
        result.setdefault(row[0], {})[row[1]] = row[2]
    for row in connection.execute(
        f"SELECT {source},COUNT(*),COUNT(DISTINCT filing_key || char(10) || url) FROM dossier_edges"
        + where
        + (" AND" if where else " WHERE")
        + " state='outside_scope' GROUP BY 1",
        args,
    ):
        result.setdefault(row[0], {}).update(outside_scope_edges=row[1], outside_scope_attachments=row[2])
    return result


def discovery_attempt_counts(connection: sqlite3.Connection, region: str | None) -> list[dict[str, Any]]:
    where, args = (" WHERE COALESCE(q.region,l.region)=?", (region,)) if region else ("", ())
    rows = connection.execute(
        "SELECT a.kind,a.state,COUNT(*),COALESCE(SUM(a.requests),0),SUM(a.duration_s),COUNT(a.duration_s) "
        "FROM discovery_attempts a LEFT JOIN discovery_queries q ON a.kind='query' AND q.key=a.object_key "
        "LEFT JOIN discovery_leads l ON a.kind='resolve' AND l.id=a.object_key" + where + " GROUP BY a.kind,a.state",
        args,
    )
    return [
        {
            "kind": row[0],
            "state": row[1],
            "attempts": row[2],
            "physical_requests": row[3],
            "recorded_duration_s": row[4],
            "attempts_with_duration": row[5],
        }
        for row in rows
    ]
