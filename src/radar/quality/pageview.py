"""The page of a PDF where a value was read, rendered with the passage highlighted: the reviewer checks a value
against its source without opening and searching the document.

Locating: a form-field locator (`field:<name>`) highlights that field; otherwise the quote is searched in the text
layer, in fragments (quotes are whitespace-squashed, dehyphenated and person-masked), then by 5-word windows.
Rendering is in memory only (never stored); person names in the text layer are blacked out on the page (images are
kept, so scanned pages stay readable: the reviewer could open the public document anyway).
"""

import re
from dataclasses import dataclass
from typing import Any

import pymupdf

from radar.domain.privacy import EMAIL_MASK, MASK, PHONE_MASK, person_names
from radar.domain.text import fold, squash
from radar.quality.corpus import redact_page_text
from radar.reading.pdf import read_pdf

ZOOM = 2.0  # ~1190 x 1684 px for A4: small print stays legible
WINDOW = 5  # words per fallback search window
MIN_FRAGMENT = 12  # shorter fragments match anywhere
_COORDINATE = re.compile(r"coordonn|^lat|^long")


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
    hint = int(locator[5:]) - 1 if re.fullmatch(r"page:\d+", locator) else 0
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


def render(data: bytes, quote: str, locator: str) -> PageView:
    names = person_names(read_pdf(data).fields)
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        n, rects, fields = locate(doc, quote, locator)
        page = doc[n]
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
