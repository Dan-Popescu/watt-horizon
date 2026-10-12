"""Polite HTTP client: per-host pacing, stop-on-block, bounded retries, OS trust store for TLS.

Rules learned the hard way (2026-10-12): five parallel crawlers made the shared prefecture platform close
connections for hours. Hence: one client, per-host minimum interval, and `SiteBlockedError` stops a whole run.
"""

import json
import ssl
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import httpx
import truststore


class SiteBlockedError(RuntimeError):
    """The host closed the connection or answered 403/429: stop collecting from it for this run."""

    def __init__(self, host: str, reason: str) -> None:
        super().__init__(f"{host}: {reason}")
        self.host = host


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
    ) -> None:
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_s,
            follow_redirects=True,
            verify=ctx,
            transport=transport,
        )
        self.default_interval_s = default_interval_s
        self.retries = retries
        self._clock, self._sleep = clock, sleep
        self._last_request: dict[str, float] = {}
        self.blocked_hosts: set[str] = set()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteHttp":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _pace(self, host: str, interval: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = last + interval - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def get_bytes(self, url: str, min_interval_s: float | None = None) -> bytes:
        host = urllib.parse.urlparse(url).netloc
        if host in self.blocked_hosts:
            raise SiteBlockedError(host, "already blocked in this run")
        interval = self.default_interval_s if min_interval_s is None else min_interval_s
        for attempt in range(self.retries + 1):
            self._pace(host, interval)
            try:
                response = self._client.get(url)
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
                self.blocked_hosts.add(host)
                raise SiteBlockedError(host, f"connection closed ({type(exc).__name__})") from exc
            except httpx.TimeoutException:
                if attempt == self.retries:
                    raise
                self._sleep(interval * (2**attempt) * 5)
                continue
            if response.status_code in (403, 429):
                self.blocked_hosts.add(host)
                raise SiteBlockedError(host, f"HTTP {response.status_code}")
            if response.status_code >= 500 and attempt < self.retries:
                self._sleep(interval * (2**attempt) * 5)
                continue
            response.raise_for_status()
            return response.content
        raise AssertionError("unreachable")  # pragma: no cover

    def get_text(self, url: str, min_interval_s: float | None = None) -> str:
        return self.get_bytes(url, min_interval_s).decode("utf-8", errors="replace")

    def get_json(self, url: str, min_interval_s: float | None = None) -> Any:
        return json.loads(self.get_bytes(url, min_interval_s))
