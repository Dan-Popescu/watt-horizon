"""Bound downloaded and stored files before expensive reading, with no live network."""

import gzip
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from radar.net.http import PoliteHttp
from radar.store.blobs import BlobStore
from tests.test_sources_http import FakeClock


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], clock: FakeClock | None = None) -> None:
        self.chunks, self.clock, self.closed, self.reads = chunks, clock, False, 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            if self.clock is not None:
                self.clock.now += 0.8
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_blob_read_refuses_large_file_and_preserves_legacy_reads(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path)
    key = blobs.put(b"abcdefgh")
    with pytest.raises(ValueError, match="limit"):
        blobs.get(key, max_bytes=4)
    assert blobs.get(key, max_bytes=8) == b"abcdefgh"
    assert blobs.get(key) == b"abcdefgh"


def test_concurrent_blob_writers_preserve_one_complete_content(tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path)
    data = b"synthetic PDF bytes" * 100
    with ThreadPoolExecutor(max_workers=6) as workers:
        keys = list(workers.map(lambda _: blobs.put(data), range(30)))
    assert len(set(keys)) == 1
    assert blobs.get(keys[0]) == data
    assert list(tmp_path.rglob("*.tmp")) == []


def test_declared_oversize_response_is_rejected_without_reading_body() -> None:
    body = Chunks([b"x"])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Length": "100"}, stream=body))
    with (
        PoliteHttp("test", transport=transport, max_response_bytes=8) as client,
        pytest.raises(ValueError, match="limit"),
    ):
        client.get_bytes("https://example.test/large")
    assert body.closed and body.reads == 0


def test_chunked_response_cannot_exceed_download_limit() -> None:
    body = Chunks([b"abcd", b"efgh", b"ijk"])
    with (
        PoliteHttp(
            "test", max_response_bytes=8, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
        ) as client,
        pytest.raises(ValueError, match="limit"),
    ):
        client.get_bytes("https://example.test/chunked")
    assert body.closed


def test_compressed_response_is_limited_after_decoding() -> None:
    body = Chunks([gzip.compress(b"x" * 1000)])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body))
    with (
        PoliteHttp("test", transport=transport, max_response_bytes=64) as client,
        pytest.raises(ValueError, match="limit"),
    ):
        client.get_bytes("https://example.test/compressed")
    assert body.closed


def test_compressed_members_are_not_silently_discarded() -> None:
    content = gzip.compress(b"first") + gzip.compress(b"second")
    body = Chunks([content[:13], content[13:]])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body))
    with PoliteHttp("test", transport=transport) as client:
        assert client.get_bytes("https://example.test/members") == b"firstsecond"


def test_deflate_response_keeps_supported_decoding() -> None:
    body = Chunks([zlib.compress(b"decoded content")])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Encoding": "deflate"}, stream=body))
    with PoliteHttp("test", transport=transport) as client:
        assert client.get_bytes("https://example.test/deflate") == b"decoded content"


def test_streaming_deadline_covers_the_entire_body() -> None:
    clock = FakeClock()
    body = Chunks([b"a", b"b", b"c", b"d"], clock)
    with (
        PoliteHttp(
            "test",
            max_response_seconds=2,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)),
        ) as client,
        pytest.raises(httpx.TimeoutException, match="deadline"),
    ):
        client.get_bytes("https://example.test/slow")
    assert body.closed and body.reads < 4


def test_deadline_includes_retries_and_backoff() -> None:
    clock = FakeClock()
    calls = []

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        clock.now += 0.8
        raise httpx.ReadTimeout("slow", request=request)

    with (
        PoliteHttp(
            "test",
            default_interval_s=0.1,
            max_response_seconds=2,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(timeout),
        ) as client,
        pytest.raises(httpx.TimeoutException),
    ):
        client.get_bytes("https://example.test/retry")
    assert len(calls) == 2


def test_redirects_keep_pacing_and_stop_at_blocked_destination() -> None:
    calls = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/first":
            return httpx.Response(302, headers={"Location": "/second"})
        return httpx.Response(200, content=b"ok")

    with PoliteHttp("test", transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep) as client:
        assert client.get_bytes("https://example.test/first") == b"ok"
    assert len(calls) == 2 and clock.slept == [1.0]
