"""PDF reading: form fields (most reliable) > text layer > scanned pages flagged for OCR."""

import base64
import json
import os
import re
import sys
import zlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property

import pymupdf

from radar.domain.text import squash
from radar.reading.workers import run_bounded
from radar.store.cache import KeyValueCache

MIN_TEXT_CHARS_PER_PAGE = 200  # below this, a page is considered a scan
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_TEXT_CHARS = 2_000_000


@dataclass(frozen=True)
class PdfContent:
    pages: int
    fields: dict[str, str]  # filled AcroForm fields, first non-empty value per name
    page_texts: tuple[str, ...]
    scanned_pages: tuple[int, ...]  # 1-based page numbers without a usable text layer
    ocr_texts: dict[int, str] = field(default_factory=dict)  # 1-based page -> OCR text, filled by an OcrEngine
    ocr_engines: dict[int, str] = field(default_factory=dict)  # 1-based page -> engine label (provenance)
    field_names: tuple[str, ...] = ()  # every AcroForm field, filled or not (template fingerprint)
    ocr_errors: dict[int, str] = field(default_factory=dict)  # unread is explicit, including resource-limit reasons

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


def _read_pdf(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> PdfContent:
    if len(data) > max_bytes:
        raise ValueError("PDF exceeds byte limit")
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        if doc.page_count > max_pages:
            raise ValueError("PDF exceeds page limit")
        fields: dict[str, str] = {}
        names: set[str] = set()
        texts: list[str] = []
        scanned: list[int] = []
        text_chars = 0
        for page in doc:
            for widget in page.widgets() or []:
                name, value = (widget.field_name or "").strip(), widget.field_value
                if name:
                    names.add(name)
                if name and value not in (None, "", "Off") and name not in fields:
                    fields[name] = str(value).replace("\r", "\n").strip()
            text = page.get_text()
            text_chars += len(text)
            if text_chars > max_text_chars:
                raise ValueError("PDF exceeds text limit")
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


def read_pdf(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    document_timeout_s: float = 30,
) -> PdfContent:
    """Parse untrusted bytes in an owned, terminable process with explicit resource limits."""
    if len(data) > max_bytes:
        raise ValueError("PDF exceeds byte limit")
    args = (data, max_pages, max_text_chars, max_bytes)
    _, result = next(run_bounded(_parse_encoded, [("document", args)], max_workers=1, timeout_s=document_timeout_s))
    if result.error or result.value is None:
        raise ValueError(result.error or "PDF worker returned no content")
    return _decode(result.value)


# Bump when read_pdf's output can change (with the PyMuPDF version, it keys the reading cache).
READER_VERSION = f"1:{pymupdf.VersionBind}:{MIN_TEXT_CHARS_PER_PAGE}"


PARALLEL_MIN = 24  # documents to parse before a process pool pays off
MAX_WORKERS = 2
CHUNK = 64


class PdfReader:
    """`read_pdf` with a persistent cache (derived.sqlite, compressed) keyed by document hash + reader version,
    and a small in-process memo: parsing is most of the build time and the same PDF is read by several steps.
    The cache holds the raw text (like `blobs/`): local only, deletable at any time."""

    NAMESPACE = "pdf"
    MEMO = 256

    def __init__(
        self,
        cache: KeyValueCache | None = None,
        *,
        max_workers: int = MAX_WORKERS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_batch_bytes: int = 64 * 1024 * 1024,
        max_memo_bytes: int = 32 * 1024 * 1024,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        document_timeout_s: float = 30,
    ) -> None:
        if min(max_workers, max_bytes, max_pages, max_batch_bytes, max_memo_bytes, max_text_chars) <= 0:
            raise ValueError("PDF resource limits must be positive")
        if max_batch_bytes < max_bytes:
            raise ValueError("PDF batch byte limit must accommodate one document")
        self.cache = cache
        self.max_workers, self.max_bytes, self.max_pages = max_workers, max_bytes, max_pages
        self.max_batch_bytes, self.max_memo_bytes, self.max_text_chars = max_batch_bytes, max_memo_bytes, max_text_chars
        if document_timeout_s <= 0:
            raise ValueError("PDF timeout must be positive")
        self.document_timeout_s = document_timeout_s
        self.prefetch_errors: dict[str, str] = {}
        self._memo: OrderedDict[str, PdfContent] = OrderedDict()
        self._memo_sizes: dict[str, int] = {}
        self._memo_bytes = 0

    def prefetch(self, documents: list[tuple[str, Callable[[], bytes]]], workers: int | None = None) -> int:
        """Parse in parallel the documents missing from the cache (CPU only: no network, same output as `read`).
        Below PARALLEL_MIN documents, starting processes (~4 s each on Windows) costs more than it saves."""
        self.prefetch_errors = {}
        if self.cache is None:
            return 0
        unique = dict(documents)
        missing = [
            (doc_id, load)
            for doc_id, load in unique.items()
            if doc_id not in self._memo and self.cache.get(self.NAMESPACE, f"{doc_id}:{READER_VERSION}") is None
        ]
        workers = min(workers or self.max_workers, self.max_workers, max(1, (os.cpu_count() or 2) - 1))
        if len(missing) < PARALLEL_MIN or workers < 2:
            return 0
        completed = 0

        def store_batch(batch: list[tuple[str, bytes]]) -> int:
            jobs = [(doc_id, (data, self.max_pages, self.max_text_chars, self.max_bytes)) for doc_id, data in batch]
            count = 0
            for doc_id, result in run_bounded(
                _parse_encoded, jobs, max_workers=workers, timeout_s=self.document_timeout_s
            ):
                if result.error or result.value is None:
                    self.prefetch_errors[doc_id] = result.error or "PDF worker returned no content"
                    continue
                assert self.cache is not None
                self.cache.put(self.NAMESPACE, f"{doc_id}:{READER_VERSION}", result.value)
                count += 1
            return count

        batch: list[tuple[str, bytes]] = []
        batch_bytes = 0
        for doc_id, load in missing:
            try:
                data = load()
                if len(data) > self.max_bytes:
                    raise ValueError("PDF exceeds byte limit")
            except Exception as exc:
                self.prefetch_errors[doc_id] = f"{type(exc).__name__}: {exc}"
                continue
            if batch and (batch_bytes + len(data) > self.max_batch_bytes or len(batch) >= min(CHUNK, workers * 2)):
                completed += store_batch(batch)
                batch, batch_bytes = [], 0
            batch.append((doc_id, data))
            batch_bytes += len(data)
        if batch:
            completed += store_batch(batch)
        return completed

    def read(self, document_id: str, data: bytes) -> PdfContent:
        if len(data) > self.max_bytes:
            raise ValueError("PDF exceeds byte limit")
        if (hit := self._memo.get(document_id)) is not None:
            self._memo.move_to_end(document_id)
            return hit
        key = f"{document_id}:{READER_VERSION}"
        stored = self.cache.get(self.NAMESPACE, key) if self.cache is not None else None
        if stored is not None:
            content = _decode(stored)
        else:
            content = read_pdf(
                data,
                max_bytes=self.max_bytes,
                max_pages=self.max_pages,
                max_text_chars=self.max_text_chars,
                document_timeout_s=self.document_timeout_s,
            )
            if self.cache is not None:
                self.cache.put(self.NAMESPACE, key, _encode(content))
        if content.pages > self.max_pages:
            raise ValueError("PDF exceeds page limit")
        if sum(map(len, content.page_texts)) > self.max_text_chars:
            raise ValueError("PDF exceeds text limit")
        size = sum(sys.getsizeof(s) for s in (*content.page_texts, *content.fields.keys(), *content.fields.values()))
        size += sys.getsizeof(content.page_texts) + sys.getsizeof(content.fields) + sys.getsizeof(content)
        if size <= self.max_memo_bytes:
            self._memo[document_id] = content
            self._memo_sizes[document_id] = size
            self._memo_bytes += size
        while len(self._memo) > self.MEMO or self._memo_bytes > self.max_memo_bytes:
            removed, _ = self._memo.popitem(last=False)
            self._memo_bytes -= self._memo_sizes.pop(removed)
        return content


def _parse_encoded(
    data: bytes,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    return _encode(_read_pdf(data, max_bytes=max_bytes, max_pages=max_pages, max_text_chars=max_text_chars))


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
