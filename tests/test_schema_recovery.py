"""A failed additive migration never leaves half its tables behind."""

import sqlite3
from pathlib import Path

import pytest

from radar.store import db as database


def test_migration_and_version_advance_are_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "radar.sqlite"
    with database.Database(path):
        pass
    original_version = database.SCHEMA_VERSION
    monkeypatch.setattr(
        database, "_MIGRATIONS", (*database._MIGRATIONS, "CREATE TABLE partial_test(id INTEGER); INVALID SQL;")
    )
    monkeypatch.setattr(database, "SCHEMA_VERSION", original_version + 1)
    with pytest.raises(sqlite3.Error):
        database.Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == original_version
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='partial_test'").fetchone() is None
