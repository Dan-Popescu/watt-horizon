"""Shared physical HTTP admission for legacy commands and durable workers.

The mutex database stays locked over the response lifetime. Admission timestamps
are committed in a different database before sending, so a crashed process both
releases its OS-managed mutex and retains its conservative pacing reservation.
Neither database is the application's dataset/publication lock.
"""

import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path


class NetworkGate:
    def __init__(
        self,
        data_dir: Path,
        *,
        interval_s: float = 5,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(interval_s) or interval_s < 0:
            raise ValueError("Shared HTTP pacing must be finite and nonnegative")
        self.data_dir, self.interval_s = data_dir, interval_s
        self.clock, self.sleep = clock, sleep

    @contextmanager
    def admit(self, timeout_s: float) -> Iterator[Callable[[], None]]:
        """Serialize the entire physical response, with durable start-time pacing.

        The caller supplies its remaining transport deadline. A busy gate or a
        pacing wait never grants an extra response timeout. Local-disk SQLite
        locking coordinates processes and threads, not multiple network hosts.
        """
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise TimeoutError("Shared HTTP admission deadline exhausted")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        started = self.clock()
        deadline = started + timeout_s
        mutex = sqlite3.connect(self.data_dir / "network-lock.sqlite", timeout=timeout_s)
        try:
            try:
                mutex.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise TimeoutError("Shared HTTP admission is busy until its deadline") from exc
                raise
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("Shared HTTP admission deadline exhausted")
            state_path = self.data_dir / "network-admission.sqlite"
            with closing(sqlite3.connect(state_path, timeout=remaining)) as state, state:
                state.execute(
                    "CREATE TABLE IF NOT EXISTS admission (id INTEGER PRIMARY KEY CHECK(id=1),next_allowed REAL NOT NULL)"
                )
                row = state.execute("SELECT next_allowed FROM admission WHERE id=1").fetchone()
                allowed = float(row[0]) if row else 0
                delay = max(0.0, allowed - self.clock())
                if delay >= deadline - self.clock():
                    raise TimeoutError("Shared HTTP pacing would exceed its deadline")
                if delay:
                    self.sleep(delay)
                current = self.clock()
                if current >= deadline:
                    raise TimeoutError("Shared HTTP admission deadline exhausted")
            reserved = False

            def reserve_send() -> None:
                nonlocal reserved
                if reserved:
                    raise RuntimeError("A physical HTTP admission can only be reserved once")
                current = self.clock()
                if current >= deadline:
                    raise TimeoutError("Shared HTTP admission deadline exhausted before send")
                with closing(sqlite3.connect(state_path, timeout=deadline - current)) as state, state:
                    state.execute("PRAGMA synchronous=FULL")
                    state.execute(
                        "INSERT INTO admission VALUES (1,?) ON CONFLICT(id) DO UPDATE SET next_allowed=excluded.next_allowed",
                        (current + self.interval_s,),
                    )
                reserved = True

            # The caller completes its budget/fence checks, then commits this
            # reservation immediately before sending. The mutex spans the body.
            yield reserve_send
        finally:
            mutex.rollback()
            mutex.close()
