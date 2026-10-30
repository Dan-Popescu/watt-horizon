"""Minimal HTML helpers (regex-based: the sites are simple and stable; no heavy parser dependency)."""

import html
import re
import urllib.parse
from collections.abc import Iterator
from html.parser import HTMLParser

from radar.domain.text import squash

_LINK = re.compile(r"<a\b[^>]*?href=(['\"])(?P<href>[^'\"]+)\1[^>]*>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


class _ContentHtml(HTMLParser):
    """Remove navigation landmarks without changing links in the document body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        blocked = (
            bool(self.stack and self.stack[-1][1])
            or tag
            in {
                "header",
                "footer",
                "nav",
                "aside",
                "script",
                "style",
            }
            or dict(attrs).get("role") in {"navigation", "banner", "contentinfo", "complementary"}
        )
        if not blocked:
            self.parts.append(self.get_starttag_text() or "")
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, blocked))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(f"</{tag}>")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")


def content_html(page: str) -> str:
    parser = _ContentHtml()
    parser.feed(page)
    parser.close()
    return "".join(parser.parts)


def strip_tags(fragment: str) -> str:
    return squash(html.unescape(_TAGS.sub(" ", fragment)))


def links(page: str, base_url: str) -> Iterator[tuple[str, str, int]]:
    """Yield (absolute_url, link_text, position_in_page)."""
    for m in _LINK.finditer(page):
        yield urllib.parse.urljoin(base_url, html.unescape(m["href"])), strip_tags(m["body"]), m.start()
