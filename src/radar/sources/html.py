"""Minimal HTML helpers (regex-based: the sites are simple and stable; no heavy parser dependency)."""

import html
import re
import urllib.parse
from collections.abc import Iterator

from radar.domain.text import squash

_LINK = re.compile(r"<a\b[^>]*?href=(['\"])(?P<href>[^'\"]+)\1[^>]*>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def strip_tags(fragment: str) -> str:
    return squash(html.unescape(_TAGS.sub(" ", fragment)))


def links(page: str, base_url: str) -> Iterator[tuple[str, str, int]]:
    """Yield (absolute_url, link_text, position_in_page)."""
    for m in _LINK.finditer(page):
        yield urllib.parse.urljoin(base_url, html.unescape(m["href"])), strip_tags(m["body"]), m.start()
