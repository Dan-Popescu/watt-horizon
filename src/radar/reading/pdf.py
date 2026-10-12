"""PDF reading: form fields (most reliable) > text layer > scanned pages flagged for OCR."""

import base64
import json
import os
import re
import zlib
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import cached_property

import pymupdf

from radar.domain.text import squash
from radar.store.cache import KeyValueCache

MIN_TEXT_CHARS_PER_PAGE = 200  # below this, a page is considered a scan


@dataclass(frozen=True)
class PdfContent:
    pages: int
    fields: dict[str, str]  # filled AcroForm fields, first non-empty value per name
    page_texts: tuple[str, ...]
    scanned_pages: tuple[int, ...]  # 1-based page numbers without a usable text layer
    ocr_texts: dict[int, str] = field(default_factory=dict)  # 1-based page -> OCR text, filled by an OcrEngine
    ocr_engines: dict[int, str] = field(default_factory=dict)  # 1-based page -> engine label (provenance)
    field_names: tuple[str, ...] = ()  # every AcroForm field, filled or not (template fingerprint)

    @cached_property
    def text(self) -> str:
        """Whole document text (text layer, completed by OCR where available), whitespace-squashed."""
        parts = [self.ocr_texts.get(i + 1) or t for i, t in enumerate(self.page_texts)]
        return squash(" ".join(dehyphenate(p) for p in parts))

    @property
    def read_by(self) -> str:
        """Provenance summary: "text", or e.g. "text + transcript (p1) + tesseract-5.5.0:fra@300dpi (p2-3)"."""
        by_engine: dict[str, list[int]] = {}
        for page, label in sorted(self.ocr_engines.items()):
            by_engine.setdefault(label, []).append(page)
        parts = ["text"] if len(self.scanned_pages) < self.pages else []
        parts += [f"{label} (p{_ranges(pages)})" for label, pages in by_engine.items()]
        if self.unread_scanned_pages:
            parts.append(f"unread (p{_ranges(list(self.unread_scanned_pages))})")
        return " + ".join(parts) or "empty"

    @property
    def unread_scanned_pages(self) -> tuple[int, ...]:
        return tuple(p for p in self.scanned_pages if p not in self.ocr_texts)

    @property
    def is_mostly_scanned(self) -> bool:
        return self.pages > 0 and len(self.scanned_pages) * 2 > self.pages


# Place-name connectors keep their hyphen: « Bonningues-\nlès-Calais », « Courcelles-\nsur-Vesle ».
_CONNECTOR = r"(?:lès|les|lez|sur|sous|en|de|du|des|la|le|aux|au|et|d'|l')\b"
_SPLIT_WORD = re.compile(rf"(?<=[a-zà-ÿ])-[ \t]*\n[ \t]*(?!{_CONNECTOR})(?=[a-zà-ÿ])")
_SPLIT_COMPOUND = re.compile(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)")


def dehyphenate(text: str) -> str:
    """Line-end hyphenation: « installa-\ntion » -> « installation »; « Loon-\nPlage » -> « Loon-Plage »."""
    return _SPLIT_COMPOUND.sub("-", _SPLIT_WORD.sub("", text))


def _ranges(pages: list[int]) -> str:
    """[1, 2, 3, 5] -> "1-3,5"."""
    out: list[str] = []
    for page in pages:
        if out and page == int(out[-1].split("-")[-1]) + 1:
            out[-1] = f"{out[-1].split('-')[0]}-{page}"
        else:
            out.append(str(page))
    return ",".join(out)


def read_pdf(data: bytes) -> PdfContent:
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        fields: dict[str, str] = {}
        names: set[str] = set()
        texts: list[str] = []
        scanned: list[int] = []
        for page in doc:
            for widget in page.widgets() or []:
                name, value = (widget.field_name or "").strip(), widget.field_value
                if name:
                    names.add(name)
                if name and value not in (None, "", "Off") and name not in fields:
                    fields[name] = str(value).replace("\r", "\n").strip()
            text = page.get_text()
            texts.append(text)
            if len(text.strip()) < MIN_TEXT_CHARS_PER_PAGE:
                scanned.append(page.number + 1)
        return PdfContent(
            pages=doc.page_count,
            fields=fields,
            page_texts=tuple(texts),
            scanned_pages=tuple(scanned),
            field_names=tuple(sorted(names)),
        )


# Bump when read_pdf's output can change (with the PyMuPDF version, it keys the reading cache).
READER_VERSION = f"1:{pymupdf.VersionBind}:{MIN_TEXT_CHARS_PER_PAGE}"


PARALLEL_MIN = 24  # documents to parse before a process pool pays off
MAX_WORKERS = 8
CHUNK = 64


class PdfReader:
    """`read_pdf` with a persistent cache (derived.sqlite, compressed) keyed by document hash + reader version,
    and a small in-process memo: parsing is most of the build time and the same PDF is read by several steps.
    The cache holds the raw text (like `blobs/`): local only, deletable at any time."""

    NAMESPACE = "pdf"
    MEMO = 256

    def __init__(self, cache: KeyValueCache | None = None) -> None:
        self.cache = cache
        self._memo: OrderedDict[str, PdfContent] = OrderedDict()

    def prefetch(self, documents: list[tuple[str, Callable[[], bytes]]], workers: int | None = None) -> int:
        """Parse in parallel the documents missing from the cache (CPU only: no network, same output as `read`).
        Below PARALLEL_MIN documents, starting processes (~4 s each on Windows) costs more than it saves."""
        if self.cache is None:
            return 0
        missing = [
            (doc_id, load)
            for doc_id, load in documents
            if doc_id not in self._memo and self.cache.get(self.NAMESPACE, f"{doc_id}:{READER_VERSION}") is None
        ]
        workers = workers or min(MAX_WORKERS, max(1, (os.cpu_count() or 2) - 1))
        if len(missing) < PARALLEL_MIN or workers < 2:
            return 0
        with ProcessPoolExecutor(workers) as pool:
            for start in range(0, len(missing), CHUNK):  # bounded memory: a chunk of PDFs at a time
                chunk = missing[start : start + CHUNK]
                for (doc_id, _), encoded in zip(
                    chunk, pool.map(_parse_encoded, [load() for _, load in chunk]), strict=True
                ):
                    self.cache.put(self.NAMESPACE, f"{doc_id}:{READER_VERSION}", encoded)
        return len(missing)

    def read(self, document_id: str, data: bytes) -> PdfContent:
        if (hit := self._memo.get(document_id)) is not None:
            self._memo.move_to_end(document_id)
            return hit
        key = f"{document_id}:{READER_VERSION}"
        stored = self.cache.get(self.NAMESPACE, key) if self.cache is not None else None
        if stored is not None:
            content = _decode(stored)
        else:
            content = read_pdf(data)
            if self.cache is not None:
                self.cache.put(self.NAMESPACE, key, _encode(content))
        self._memo[document_id] = content
        if len(self._memo) > self.MEMO:
            self._memo.popitem(last=False)
        return content


def _parse_encoded(data: bytes) -> str:
    return _encode(read_pdf(data))  # runs in a worker process


def _encode(c: PdfContent) -> str:
    raw = {
        "pages": c.pages,
        "fields": c.fields,
        "texts": c.page_texts,
        "scanned": c.scanned_pages,
        "names": c.field_names,
    }
    return base64.b64encode(zlib.compress(json.dumps(raw, ensure_ascii=False).encode(), 6)).decode()


def _decode(value: str) -> PdfContent:
    raw = json.loads(zlib.decompress(base64.b64decode(value)))
    return PdfContent(
        pages=raw["pages"], fields=raw["fields"], page_texts=tuple(raw["texts"]),
        scanned_pages=tuple(raw["scanned"]), field_names=tuple(raw["names"]),
    )  # fmt: skip
