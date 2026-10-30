"""Cursor-based relation reads; decode one link or latest human decision at a time."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime

from radar.domain.models import Link, LinkDecision


class LinkQueries:
    conn: sqlite3.Connection

    def iter_links(self, key: str | None = None) -> Iterator[Link]:
        sql = "SELECT data FROM links"
        args: tuple[str, ...] = ()
        if key is not None:
            sql += " WHERE a=? OR b=?"
            args = (key, key)
        cursor = self.conn.execute(sql + " ORDER BY a,b", args)
        try:
            for row in cursor:
                yield Link.model_validate_json(row[0])
        finally:
            cursor.close()

    def iter_current_link_decisions(self, *, include_withdrawn: bool = False) -> Iterator[LinkDecision]:
        """Latest row wins, including withdrawal; never materialize append-only history."""
        where = "" if include_withdrawn else "WHERE d.verdict<>'undecided' "
        cursor = self.conn.execute(
            "SELECT d.* FROM link_decisions d JOIN "
            "(SELECT a,b,MAX(id) AS id FROM link_decisions GROUP BY a,b) latest ON latest.id=d.id "
            + where
            + "ORDER BY d.a,d.b"
        )
        try:
            for row in cursor:
                yield LinkDecision(
                    id=row["id"],
                    a=row["a"],
                    b=row["b"],
                    verdict=row["verdict"],
                    author=row["author"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    note=row["note"],
                )
        finally:
            cursor.close()
