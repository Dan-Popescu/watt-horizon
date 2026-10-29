"""Crash-safe, reentrant coordination for cooperating writers on local disk."""

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_HELD = threading.local()


class ProcessBusyError(RuntimeError):
    """Another writer owns this dataset's processing lease."""


@contextmanager
def process_lock(data_dir: Path, *, timeout_s: float = 0) -> Iterator[None]:
    """Serialize local writers; nesting in one thread does not acquire twice.

    SQLite owns the operating-system lock, so a dead process does not leave a stale
    lease to remove manually. Each thread acquires its own connection. This is a
    local-disk lock, not a distributed lease for a network filesystem.
    """
    if timeout_s < 0:
        raise ValueError("timeout_s must be nonnegative")
    directory = data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = str(directory)
    if getattr(_HELD, "pid", None) != os.getpid():
        _HELD.paths = set()
        _HELD.pid = os.getpid()
    held: set[str] = _HELD.paths
    if key in held:
        yield
        return
    connection = sqlite3.connect(directory / "process-lock.sqlite", timeout=timeout_s)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise ProcessBusyError("A processing job already owns this dataset") from exc
            raise
        held.add(key)
        _HELD.paths = held
        try:
            yield
        finally:
            held.remove(key)
            connection.rollback()
    finally:
        connection.close()
