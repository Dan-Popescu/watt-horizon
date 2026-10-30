"""Shared physical admission across real legacy and persistent-worker clients."""

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from radar import wiring
from radar.config import Settings
from radar.net.http import PoliteHttp
from radar.pipeline.tasks import PreparedWork, TaskWorker, locked
from radar.store.db import Database
from radar.store.network import NetworkGate
from radar.store.tasks import TaskSpec, TaskStore


def test_pacing_reservation_survives_failed_response_and_new_gate(tmp_path: Path) -> None:
    clock = [100.0]
    waits = []

    def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    first = NetworkGate(tmp_path, clock=lambda: clock[0], sleep=sleep)
    with pytest.raises(RuntimeError, match="crashed"), first.admit(60) as reserve:
        reserve()
        # An independent connection already observes the committed admission.
        with sqlite3.connect(tmp_path / "network-admission.sqlite") as state:
            assert state.execute("SELECT next_allowed FROM admission").fetchone()[0] == 105
        raise RuntimeError("crashed after physical admission")
    second = NetworkGate(tmp_path, clock=lambda: clock[0], sleep=sleep)
    with second.admit(60) as reserve:
        assert clock[0] == 105
        reserve()
    assert waits == [5]


@pytest.mark.parametrize("delay_location", ["local_pace", "on_request", "before_send"])
def test_reservation_follows_all_pre_send_waits(tmp_path: Path, delay_location: str) -> None:
    clock = [100.0]
    sent = []

    def sleep(delay: float) -> None:
        clock[0] += delay

    def response(_: httpx.Request) -> httpx.Response:
        sent.append(clock[0])
        return httpx.Response(200, content=b"observed")

    def delayed() -> None:
        sleep(10)

    for index in range(2):
        gate = NetworkGate(tmp_path, clock=lambda: clock[0], sleep=sleep)
        with PoliteHttp(
            "test",
            transport=httpx.MockTransport(response),
            clock=lambda: clock[0],
            sleep=sleep,
            default_interval_s=10 if delay_location == "local_pace" and index == 0 else 0,
            request_gate=gate.admit,
        ) as http:
            if delay_location == "local_pace" and index == 0:
                http._last_request["example.org"] = clock[0]
            with http.request_budget(
                1,
                on_request=delayed if delay_location == "on_request" and index == 0 else None,
                before_send=delayed if delay_location == "before_send" and index == 0 else None,
            ):
                assert http.get_bytes("https://example.org/document") == b"observed"
    assert sent == [110, 115]


def test_each_redirect_and_retry_passes_shared_gate(tmp_path: Path) -> None:
    clock = [100.0]
    sent = []

    def sleep(delay: float) -> None:
        clock[0] += delay

    def response(_: httpx.Request) -> httpx.Response:
        sent.append(clock[0])
        if len(sent) == 1:
            return httpx.Response(302, headers={"Location": "/redirect"})
        if len(sent) == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"complete")

    gate = NetworkGate(tmp_path, clock=lambda: clock[0], sleep=sleep)
    with (
        PoliteHttp(
            "test",
            transport=httpx.MockTransport(response),
            clock=lambda: clock[0],
            sleep=sleep,
            default_interval_s=0,
            retries=1,
            request_gate=gate.admit,
        ) as http,
        http.request_budget(3) as budget,
    ):
        assert http.get_bytes("https://example.org/document") == b"complete"
        assert budget.requests == 3
    assert sent == [100, 105, 110]


@pytest.mark.parametrize("suspend_while_waiting", [False, True])
def test_legacy_make_http_and_task_worker_serialize_complete_response(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch, suspend_while_waiting: bool
) -> None:
    clock = [100.0]
    sent = []
    errors = []
    legacy_body = threading.Event()
    release_body = threading.Event()
    worker_started = threading.Event()
    worker_transport = threading.Event()

    def sleep(delay: float) -> None:
        clock[0] += delay

    monkeypatch.setattr(wiring, "NetworkGate", lambda root: NetworkGate(root, clock=lambda: clock[0], sleep=sleep))
    online = replace(settings, offline=False)
    legacy, worker_http = wiring.make_http(online), wiring.make_http(online)
    assert legacy is not None and worker_http is not None
    assert legacy.request_gate is not None and worker_http.request_gate is not None

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            legacy_body.set()
            if not release_body.wait(3):
                raise RuntimeError("Observer did not release legacy response")
            yield b"legacy response"

    def legacy_response(_: httpx.Request) -> httpx.Response:
        sent.append(("legacy", clock[0]))
        return httpx.Response(200, stream=Body())

    def task_response(_: httpx.Request) -> httpx.Response:
        sent.append(("worker", clock[0]))
        worker_transport.set()
        return httpx.Response(200, content=b"worker response")

    # Both clients come from the real composition root. Only transport is replaced.
    legacy._client.close()
    legacy._client = httpx.Client(transport=httpx.MockTransport(legacy_response))
    worker_http._client.close()
    worker_http._client = httpx.Client(transport=httpx.MockTransport(task_response))
    spec = TaskSpec("work", "same-host", {}, source="dreal", network=True)
    with locked(db):
        TaskStore(db.conn).enqueue(spec, now=100)

    def legacy_collect() -> None:
        try:
            assert legacy.get_bytes("https://example.org/document") == b"legacy response"
        except BaseException as exc:
            errors.append(exc)

    def observe() -> None:
        try:
            assert worker_started.wait(3)
            assert not worker_transport.wait(0.05), "Worker sent while legacy body was still being read"
            if suspend_while_waiting:
                with Database(db.path) as controller:
                    controller.conn.execute("PRAGMA busy_timeout=100")
                    with locked(controller):
                        TaskStore(controller.conn).block_source("dreal", "blocked while waiting", now=clock[0])
        except BaseException as exc:
            errors.append(exc)
        finally:
            release_body.set()

    collecting = threading.Thread(target=legacy_collect)
    observer = threading.Thread(target=observe)
    try:
        collecting.start()
        assert legacy_body.wait(3)
        observer.start()

        def compute(_: object) -> PreparedWork:
            worker_started.set()
            assert worker_http.get_bytes("https://example.org/document") == b"worker response"
            return PreparedWork(TaskStore(db.conn).revision(), {"downloaded": True}, lambda _: None)

        outcome = TaskWorker(
            db, {"work": compute}, offline=False, http=worker_http, clock=lambda: clock[0], sleep=sleep
        ).run_one()
        collecting.join(3)
        observer.join(3)
        assert not errors
        assert not collecting.is_alive() and not observer.is_alive()
        assert outcome and outcome["state"] == ("discarded" if suspend_while_waiting else "complete")
        assert sent == ([("legacy", 100)] if suspend_while_waiting else [("legacy", 100), ("worker", 105)])
    finally:
        release_body.set()
        collecting.join(3)
        if observer.ident is not None:
            observer.join(3)
        legacy.close()
        worker_http.close()
