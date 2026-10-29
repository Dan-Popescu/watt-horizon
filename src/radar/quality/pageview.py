"""The page of a PDF where a value was read, rendered with the passage highlighted: the reviewer checks a value
against its source without opening and searching the document.

Locating: a form-field locator (`field:<name>`) highlights that field; otherwise the quote is searched in the text
layer, in fragments (quotes are whitespace-squashed, dehyphenated and person-masked), then by 5-word windows.
Rendering is in memory only (never stored); person names in the text layer are blacked out on the page (images are
kept, so scanned pages stay readable: the reviewer could open the public document anyway).
"""

import re
from dataclasses import dataclass
from math import isfinite
from threading import BoundedSemaphore
from typing import Any

import pymupdf

from radar.domain.privacy import EMAIL_MASK, MASK, PHONE_MASK, person_names
from radar.domain.text import fold, squash
from radar.quality.corpus import redact_page_text
from radar.reading.pdf import DEFAULT_MAX_BYTES, DEFAULT_MAX_PAGES, DEFAULT_MAX_TEXT_CHARS
from radar.reading.workers import run_bounded

ZOOM = 2.0  # ~1190 x 1684 px for A4: small print stays legible
WINDOW = 5  # words per fallback search window
MIN_FRAGMENT = 12  # shorter fragments match anywhere
MAX_RENDER_PIXELS = 8_000_000
MAX_SOURCE_IMAGE_PIXELS = 40_000_000
MAX_WIDGETS = 10_000
MAX_HIGHLIGHTS = 5_000
MAX_QUOTE_CHARS = 4_000
_RENDER_SLOT = BoundedSemaphore(1)  # The web process must not multiply native image workers under concurrent reviews.
_COORDINATE = re.compile(r"coordonn|^lat|^long")


class PageViewBusyError(RuntimeError):
    """The single evidence renderer is occupied; callers can return a retryable HTTP response."""


@dataclass(frozen=True)
class PageView:
    png: bytes
    page: int  # 1-based
    pages: int
    found: bool  # False: page from the locator, passage not located (scanned page, title...)
    top: float  # highlight top as a fraction of page height (for scrolling), 0 if not found


def _fragments(quote: str) -> list[str]:
    parts = re.split("|".join(re.escape(m) for m in (MASK, EMAIL_MASK, PHONE_MASK)), quote)
    return sorted((p.strip(" ,;:.«»") for p in parts if len(p.strip()) >= MIN_FRAGMENT), key=len, reverse=True)


def _windows(quote: str) -> list[str]:
    words = [w for w in quote.split() if w not in MASK.split()]
    return [" ".join(words[i : i + WINDOW]) for i in range(0, len(words), WINDOW)]


def locate(doc: Any, quote: str, locator: str) -> tuple[int, list[Any], bool]:
    """(0-based page, rects to highlight, rects are form fields); no rects if the passage was not found."""
    hint = max(0, int(locator[5:]) - 1) if re.fullmatch(r"page:\d+", locator) else 0
    fragments = _fragments(quote)
    widgets = [(page.number, w) for page in doc for w in page.widgets() or []]
    for n, widget in widgets:
        if locator == f"field:{widget.field_name}":
            return n, [widget.rect], True
    order = sorted(range(doc.page_count), key=lambda n: n != hint)
    for fragment in fragments:
        for n in order:
            if hits := doc[n].search_for(fragment):
                return n, hits, False
    for n, widget in widgets:  # « field:description »: the quote is a passage of a field's value
        if fragments and fragments[0] in squash(str(widget.field_value or "")):
            return n, [widget.rect], True
    if locator.startswith("field:"):  # « field:coordinates »: the quote joins several fields' values
        parts = [(n, w) for n, w in widgets if len(v := squash(str(w.field_value or ""))) >= 4 and v in quote]
        if not parts and locator == "field:coordinates":  # degrees / minutes / seconds: one 2-digit field each
            parts = [(n, w) for n, w in widgets if _COORDINATE.search(fold(w.field_name or "")) and w.field_value]
        if parts:
            return parts[0][0], [w.rect for n, w in parts if n == parts[0][0]], True
    best: tuple[int, list[Any], bool] = (min(hint, doc.page_count - 1), [], False)
    for n in order:  # line-end hyphenation or OCR differences: the page where most windows match
        hits = [r for w in _windows(quote) if len(w) >= MIN_FRAGMENT for r in doc[n].search_for(w)]
        if len(hits) > len(best[1]):
            best = (n, hits, False)
    return best


def _checked_person_names(doc: Any, max_text_chars: int) -> set[str]:
    """Bound fields and text before search/highlight code materializes its candidate widgets."""
    fields: dict[str, str] = {}
    characters = widgets = 0
    for page in doc:
        characters += len(page.get_text())
        if characters > max_text_chars:
            raise ValueError("PDF exceeds text limit")
        for widget in page.widgets() or []:
            widgets += 1
            if widgets > MAX_WIDGETS:
                raise ValueError("PDF exceeds form widget limit")
            name, value = (widget.field_name or "").strip(), widget.field_value
            if name and value not in (None, "", "Off") and name not in fields:
                fields[name] = str(value)
                characters += len(name) + len(fields[name])
                if characters > max_text_chars:
                    raise ValueError("PDF exceeds text limit")
    return person_names(fields)


def _render_native(
    data: bytes,
    quote: str,
    locator: str,
    max_pages: int,
    max_pixels: int,
    max_source_image_pixels: int,
    max_text_chars: int,
) -> PageView:
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        if doc.page_count < 1:
            raise ValueError("PDF has no pages")
        if doc.page_count > max_pages:
            raise ValueError("PDF exceeds page limit")
        names = _checked_person_names(doc, max_text_chars)
        n, rects, fields = locate(doc, quote, locator)
        page = doc[n]
        if page.rect.is_empty or page.rect.width * page.rect.height * ZOOM * ZOOM > max_pixels:
            raise ValueError("PDF evidence page exceeds rendered pixel limit")
        # A small output page can still embed a decompression-bomb image; check the source dimensions first.
        source_pixels = sum(int(image[2]) * int(image[3]) for image in page.get_images(full=True))
        if source_pixels > max_source_image_pixels:
            raise ValueError("PDF evidence page exceeds source image pixel limit")
        if len(rects) > MAX_HIGHLIGHTS:
            raise ValueError("PDF evidence page exceeds highlight limit")
        redact_page_text(page, names)
        for rect in rects:
            if fields:  # a frame: a highlight would hide the field's own content
                annot = page.add_rect_annot(pymupdf.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2))  # type: ignore[no-untyped-call]
                annot.set_colors(stroke=(1, 0.5, 0))
                annot.set_border(width=2.5)
                annot.update()
            else:
                page.add_highlight_annot(rect)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))  # type: ignore[no-untyped-call]
        top = min(r.y0 for r in rects) / page.rect.height if rects else 0.0
        return PageView(png=pix.tobytes("png"), page=n + 1, pages=doc.page_count, found=bool(rects), top=top)


def render(
    data: bytes,
    quote: str,
    locator: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_pixels: int = MAX_RENDER_PIXELS,
    max_source_image_pixels: int = MAX_SOURCE_IMAGE_PIXELS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    document_timeout_s: float = 15,
) -> PageView:
    """Return a bounded in-memory image; all native search, redaction and rendering run under a watchdog."""
    if min(max_bytes, max_pages, max_pixels, max_source_image_pixels, max_text_chars) <= 0:
        raise ValueError("PDF evidence resource limits must be positive")
    if not isfinite(document_timeout_s) or document_timeout_s <= 0:
        raise ValueError("PDF evidence timeout must be positive and finite")
    if len(data) > max_bytes:
        raise ValueError("PDF exceeds byte limit")
    if len(quote) > MAX_QUOTE_CHARS or len(locator) > 512:
        raise ValueError("PDF evidence locator or quote exceeds size limit")
    if not _RENDER_SLOT.acquire(blocking=False):
        raise PageViewBusyError("PDF evidence renderer is busy")
    try:
        args = (data, quote, locator, max_pages, max_pixels, max_source_image_pixels, max_text_chars)
        _, result = next(run_bounded(_render_native, [("evidence", args)], max_workers=1, timeout_s=document_timeout_s))
        if result.error or result.value is None:
            raise ValueError(result.error or "PDF evidence worker returned no image")
        return result.value
    finally:
        _RENDER_SLOT.release()
