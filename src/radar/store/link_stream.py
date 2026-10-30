"""Disposable disk-backed relations for bounded-memory graph consumers.

This is computation scratch, not a second durable link store. The dataset caller
publishes its complete result in the existing Database transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from radar.domain.models import Link, LinkDecision


class LinkSpool:
    def __init__(self) -> None:
        self._directory = TemporaryDirectory(prefix="radar-link-spool-")
        self.conn = sqlite3.connect(Path(self._directory.name) / "links.sqlite")
        self.conn.execute("PRAGMA cache_size=-2048")
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("CREATE TABLE links(a TEXT,b TEXT,data TEXT,verdict TEXT,PRIMARY KEY(a,b))")
        self.conn.execute("CREATE INDEX links_b ON links(b)")
        self.conn.execute("CREATE TABLE human_constraints(a TEXT,b TEXT,verdict TEXT,PRIMARY KEY(a,b))")

    def __enter__(self) -> LinkSpool:
        return self

    def __exit__(self, *_: object) -> None:
        self.conn.close()
        self._directory.cleanup()

    def add(self, links: Iterable[Link]) -> None:
        """Stream every relation; the last duplicate has priority, like Database.replace_links."""
        self.conn.executemany(
            "INSERT INTO links VALUES (?,?,?,?) ON CONFLICT(a,b) "
            "DO UPDATE SET data=excluded.data,verdict=excluded.verdict",
            ((link.a, link.b, link.model_dump_json(), link.verdict) for link in links),
        )

    def apply_decisions(self, decisions: Mapping[tuple[str, str], LinkDecision]) -> None:
        """Use indexed point updates, with no Python collection of all proposed pairs."""
        self.conn.execute("DELETE FROM human_constraints")
        self.conn.executemany(
            "INSERT INTO human_constraints VALUES(?,?,?)",
            ((a, b, decision.verdict) for (a, b), decision in decisions.items()),
        )
        for (a, b), decision in sorted(decisions.items()):
            row = self.conn.execute("SELECT data FROM links WHERE a=? AND b=?", (a, b)).fetchone()
            if row:
                link = Link.model_validate_json(row[0]).model_copy(update={"verdict": decision.verdict})
            elif decision.verdict == "same_project":
                link = Link(a=a, b=b, reasons=("lien posé par un relecteur",), verdict=decision.verdict)
            else:
                continue
            self.add((link,))

    def __iter__(self) -> Iterator[Link]:
        cursor = self.conn.execute("SELECT data FROM links ORDER BY a,b")
        try:
            for row in cursor:
                yield Link.model_validate_json(row[0])
        finally:
            cursor.close()

    def __len__(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0])

    def decision_pairs(self, verdicts: tuple[str, ...]) -> Iterator[tuple[str, str]]:
        placeholders = ",".join("?" for _ in verdicts)
        sql = (
            f"SELECT a,b FROM links WHERE verdict IN ({placeholders}) "
            f"UNION SELECT a,b FROM human_constraints WHERE verdict IN ({placeholders}) ORDER BY a,b"
        )
        for row in self.conn.execute(sql, (*verdicts, *verdicts)):
            yield row[0], row[1]

    def signal_keys_for(self, filing_keys: Iterable[str], signal_keys: set[str]) -> set[str]:
        """Keep only one project's output signals, not the dataset's full filing/signal graph."""
        result: set[str] = set()
        for key in filing_keys:
            rows = self.conn.execute(
                "SELECT b FROM links WHERE a=? AND (verdict IS NULL OR verdict<>'unrelated') "
                "UNION SELECT a FROM links WHERE b=? AND (verdict IS NULL OR verdict<>'unrelated')",
                (key, key),
            )
            result.update(row[0] for row in rows if row[0] in signal_keys)
        return result
