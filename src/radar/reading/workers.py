"""Bounded native work with a real deadline and processes that can be terminated safely.

Each job has an isolated spawned process. This costs startup time on a cache miss but avoids leaving a
stuck PDF parser or OCR call alive after reporting a timeout. Never use executor private process internals.
"""

import multiprocessing
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from math import isfinite
from multiprocessing.connection import Connection, wait
from multiprocessing.context import SpawnProcess
from time import monotonic
from typing import Any


@dataclass(frozen=True)
class WorkResult[T]:
    value: T | None = None
    error: str | None = None


@dataclass
class _Running:
    key: str
    process: SpawnProcess
    receiver: Connection
    started: float


def _perform(sender: Connection, function: Callable[..., Any], args: tuple[Any, ...]) -> None:
    try:
        try:
            result = WorkResult(value=function(*args))
        except Exception as exc:
            result = WorkResult(error=f"{type(exc).__name__}: {exc}")
        sender.send(result)
    finally:
        sender.close()


def _stop(job: _Running) -> None:
    if job.process.is_alive():
        job.process.terminate()
    job.process.join(timeout=1)
    if job.process.is_alive():
        job.process.kill()
        job.process.join(timeout=1)
    job.receiver.close()
    job.process.close()


def run_bounded[T](
    function: Callable[..., T],
    jobs: Iterable[tuple[str, tuple[Any, ...]]],
    *,
    max_workers: int,
    timeout_s: float,
    total_timeout_s: float | None = None,
) -> Iterator[tuple[str, WorkResult[T]]]:
    """Yield each job's value/error, including crashes and timeouts; keep at most max_workers payloads active.

    The timeout includes spawned-worker startup. Callers should use realistic budgets (seconds, not ms).
    Closing the iterator also terminates every owned worker; no live work survives a timed-out document.
    """
    if (
        max_workers < 1
        or not isfinite(timeout_s)
        or timeout_s <= 0
        or (total_timeout_s is not None and (not isfinite(total_timeout_s) or total_timeout_s <= 0))
    ):
        raise ValueError("worker count and timeout must be positive")
    context = multiprocessing.get_context("spawn")
    pending = iter(jobs)
    running: list[_Running] = []
    exhausted = False
    group_started = monotonic()

    def group_remaining() -> float:
        return (
            max(0.0, total_timeout_s - (monotonic() - group_started)) if total_timeout_s is not None else float("inf")
        )

    try:
        while running or not exhausted:
            while not exhausted and len(running) < max_workers:
                if group_remaining() <= 0:
                    for key, _ in pending:
                        yield key, WorkResult(error="TimeoutError: worker group deadline exceeded")
                    exhausted = True
                    break
                try:
                    key, args = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(target=_perform, args=(sender, function, args), daemon=True)
                started = monotonic()
                try:
                    process.start()
                except Exception as exc:
                    sender.close()
                    receiver.close()
                    yield key, WorkResult(error=f"{type(exc).__name__}: {exc}")
                    continue
                sender.close()
                running.append(_Running(key, process, receiver, started))
            if not running:
                continue
            until_deadline = min(
                group_remaining(), min(max(0.0, timeout_s - (monotonic() - job.started)) for job in running)
            )
            ready = set(wait([job.receiver for job in running], timeout=min(0.1, until_deadline)))
            for job in list(running):
                result: WorkResult[T] | None = None
                if job.receiver in ready:
                    try:
                        result = job.receiver.recv()
                    except (EOFError, OSError):
                        result = WorkResult(error="WorkerError: native process exited without a result")
                elif monotonic() - job.started >= timeout_s:
                    result = WorkResult(error="TimeoutError: worker deadline exceeded")
                elif group_remaining() <= 0:
                    result = WorkResult(error="TimeoutError: worker group deadline exceeded")
                elif not job.process.is_alive():
                    result = WorkResult(error="WorkerError: native process exited without a result")
                if result is not None:
                    running.remove(job)
                    _stop(job)
                    yield job.key, result
    finally:
        for job in running:
            _stop(job)
