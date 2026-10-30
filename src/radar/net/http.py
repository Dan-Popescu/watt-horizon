"""Polite HTTP client: per-host pacing, stop-on-block, bounded retries, OS trust store for TLS.

Rules learned the hard way (2026-10-12): five parallel crawlers made the shared prefecture platform close
connections for hours. Hence: one client, per-host minimum interval, and `SiteBlockedError` stops a whole run.
"""

import json
import math
import ssl
import time
import urllib.parse
import zlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

import httpx
import truststore


class SiteBlockedError(RuntimeError):
    """The host closed the connection or answered 403/429: stop collecting from it for this run."""

    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{host}: {reason}")
        self.host = host


class ResponseLimitError(ValueError):
    """A download exceeded its byte budget; retrying it unchanged cannot help."""


class ResponseDeadlineError(httpx.TimeoutException):
    """The budget for the whole operation, including redirects and retries, is exhausted."""


class RequestBudgetError(ValueError):
    """No physical HTTP attempt may be started within the current scoped budget."""


@dataclass
class RequestBudget:
    limit: int
    requests: int = 0
    on_request: Callable[[], None] | None = None
    before_send: Callable[[], None] | None = None


class _RetryResponseError(Exception):
    """A server error may be retried within the original deadline."""


class PoliteHttp:
    def __init__(
        self,
        user_agent: str,
        default_interval_s: float = 1.0,
        timeout_s: float = 60.0,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_response_bytes: int = 32 * 1024 * 1024,
        max_response_seconds: float = 60.0,
        global_interval_s: float = 0.0,
        request_gate: Callable[[float], AbstractContextManager[Callable[[], None]]] | None = None,
    ) -> None:
        if (
            max_response_bytes <= 0
            or retries < 0
            or not math.isfinite(max_response_seconds)
            or max_response_seconds <= 0
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
            or not math.isfinite(default_interval_s)
            or default_interval_s < 0
            or not math.isfinite(global_interval_s)
            or global_interval_s < 0
        ):
            raise ValueError("HTTP limits must be positive and pacing cannot be negative")
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout_s,
            follow_redirects=False,
            verify=ctx,
            transport=transport,
        )
        self.default_interval_s = default_interval_s
        self.global_interval_s = global_interval_s
        self._last_global_request: float | None = None
        self.retries = retries
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self.max_response_seconds = max_response_seconds
        self._clock, self._sleep = clock, sleep
        self._last_request: dict[str, float] = {}
        self.blocked_hosts: set[str] = set()
        self._request_budgets: list[RequestBudget] = []
        self.request_gate = request_gate

    @contextmanager
    def request_budget(
        self,
        max_requests: int,
        *,
        on_request: Callable[[], None] | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> Iterator[RequestBudget]:
        """Nestable admission on physical attempts, including redirects and retries.

        Observers may durably record an attempt before sending it. A process crash
        after admission leaves a conservative request count, never a free retry.
        This serialized client does not support concurrent use across threads.
        """
        if max_requests < 0:
            raise ValueError("HTTP request budget cannot be negative")
        budget = RequestBudget(max_requests, on_request=on_request, before_send=before_send)
        self._request_budgets.append(budget)
        try:
            yield budget
        finally:
            if self._request_budgets[-1] is not budget:
                raise RuntimeError("HTTP request budgets must exit in nesting order")
            self._request_budgets.pop()

    def _admit_request(self) -> None:
        if any(budget.requests >= budget.limit for budget in self._request_budgets):
            raise RequestBudgetError("Physical HTTP request budget exhausted")
        for budget in self._request_budgets:
            if budget.on_request:
                budget.on_request()
            budget.requests += 1

    def _before_send(self) -> None:
        for budget in self._request_budgets:
            if budget.before_send:
                budget.before_send()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteHttp":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _remaining(self, deadline: float, url: str) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ResponseDeadlineError("HTTP response deadline exhausted", request=httpx.Request("GET", url))
        return remaining

    def _sleep_within(self, duration: float, deadline: float, url: str) -> None:
        if duration >= self._remaining(deadline, url):
            raise ResponseDeadlineError(
                "HTTP backoff would exceed response deadline", request=httpx.Request("GET", url)
            )
        self._sleep(duration)

    def _pace(self, host: str, interval: float, deadline: float, url: str) -> None:
        last = self._last_request.get(host)
        now = self._clock()
        wait = max(
            last + interval - now if last is not None else 0,
            self._last_global_request + self.global_interval_s - now if self._last_global_request is not None else 0,
        )
        if wait > 0:
            self._sleep_within(wait, deadline, url)
        self._last_request[host] = self._clock()
        self._last_global_request = self._clock()

    def _body(self, response: httpx.Response, deadline: float) -> bytes:
        url = str(response.url)
        length = response.headers.get("Content-Length", "")
        if length.isdecimal() and int(length) > self.max_response_bytes:
            raise ResponseLimitError(f"HTTP response exceeds byte limit ({self.max_response_bytes})")
        # MockTransport (and some custom transports) may return an already consumed
        # response. Real socket responses remain streamed below.
        if response.is_stream_consumed:
            self._remaining(deadline, url)
            if len(response.content) > self.max_response_bytes:
                raise ResponseLimitError(f"HTTP response exceeds byte limit ({self.max_response_bytes})")
            return response.content
        encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise httpx.DecodingError(
                f"Unsupported bounded HTTP content encoding: {encoding}", request=response.request
            )
        decoder = zlib.decompressobj(zlib.MAX_WBITS | 16) if encoding == "gzip" else None
        prefix = b""
        result = bytearray()
        raw_size = 0
        for raw in response.iter_raw():
            self._remaining(deadline, url)
            raw_size += len(raw)
            if raw_size > self.max_response_bytes:
                raise ResponseLimitError(f"HTTP response exceeds byte limit ({self.max_response_bytes})")
            if encoding == "deflate" and decoder is None:
                prefix += raw
                if len(prefix) < 2:
                    continue
                # Both RFC zlib-wrapped and commonly served raw deflate are accepted.
                wrapped = prefix[0] & 15 == 8 and int.from_bytes(prefix[:2], "big") % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                raw, prefix = prefix, b""
            if decoder is None:
                if len(result) + len(raw) > self.max_response_bytes:
                    raise ResponseLimitError(f"HTTP response exceeds byte limit ({self.max_response_bytes})")
                result.extend(raw)
                continue
            pending = raw
            while pending:
                self._remaining(deadline, url)
                if decoder.eof:
                    if encoding != "gzip":
                        raise httpx.DecodingError("Trailing deflate response bytes", request=response.request)
                    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
                try:
                    decoded = decoder.decompress(pending, self.max_response_bytes - len(result) + 1)
                except zlib.error as exc:
                    raise httpx.DecodingError(str(exc), request=response.request) from exc
                if len(result) + len(decoded) > self.max_response_bytes:
                    raise ResponseLimitError(f"HTTP response exceeds byte limit ({self.max_response_bytes})")
                result.extend(decoded)
                pending = decoder.unused_data
        self._remaining(deadline, url)
        if encoding != "identity" and (decoder is None or not decoder.eof):
            raise httpx.DecodingError("Incomplete compressed HTTP response", request=response.request)
        return bytes(result)

    def _fetch(
        self, url: str, interval: float, deadline: float, retry_server_error: bool, same_origin_redirects: bool = False
    ) -> bytes:
        current = url
        for _ in range(21):
            if same_origin_redirects:
                original, target = urllib.parse.urlparse(url), urllib.parse.urlparse(current)
                if (original.scheme, original.netloc.lower()) != (target.scheme, target.netloc.lower()):
                    raise ValueError("Dossier redirect leaves the selected origin")
            host = urllib.parse.urlparse(current).netloc
            if host in self.blocked_hosts:
                raise SiteBlockedError(host, "already blocked in this run")
            try:
                self._pace(host, interval, deadline, current)
                self._admit_request()
                gate = (
                    self.request_gate(self._remaining(deadline, current))
                    if self.request_gate
                    else nullcontext(lambda: None)
                )
                with gate as reserve_send:
                    self._before_send()
                    reserve_send()
                    timeout = min(self.timeout_s, self._remaining(deadline, current))
                    with self._client.stream("GET", current, timeout=timeout) as response:
                        self._remaining(deadline, current)
                        if response.status_code in (403, 429):
                            self.blocked_hosts.add(host)
                            raise SiteBlockedError(host, f"HTTP {response.status_code}")
                        if response.has_redirect_location:
                            current = str(response.url.join(response.headers["Location"]))
                            continue
                        if response.status_code >= 500 and retry_server_error:
                            raise _RetryResponseError
                        response.raise_for_status()
                        return self._body(response, deadline)
            except TimeoutError as exc:
                raise ResponseDeadlineError(str(exc), request=httpx.Request("GET", current)) from exc
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
                self.blocked_hosts.add(host)
                raise SiteBlockedError(host, f"connection closed ({type(exc).__name__})") from exc
        raise httpx.TooManyRedirects("Exceeded redirect limit", request=httpx.Request("GET", url))

    def get_bytes(self, url: str, min_interval_s: float | None = None, *, same_origin_redirects: bool = False) -> bytes:
        deadline = self._clock() + self.max_response_seconds
        interval = self.default_interval_s if min_interval_s is None else min_interval_s
        for attempt in range(self.retries + 1):
            try:
                return self._fetch(url, interval, deadline, attempt < self.retries, same_origin_redirects)
            except ResponseDeadlineError:
                raise
            except (httpx.TimeoutException, _RetryResponseError):
                if attempt == self.retries:
                    raise
                self._sleep_within(interval * (2**attempt) * 5, deadline, url)
        raise AssertionError("unreachable")  # pragma: no cover

    def get_text(self, url: str, min_interval_s: float | None = None) -> str:
        return self.get_bytes(url, min_interval_s).decode("utf-8", errors="replace")

    def get_json(self, url: str, min_interval_s: float | None = None) -> Any:
        return json.loads(self.get_bytes(url, min_interval_s))
