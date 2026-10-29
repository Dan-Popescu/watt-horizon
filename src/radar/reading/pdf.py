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
from radar.reading.tables import PdfTable, extract_tables
from radar.reading.workers import run_bounded
from radar.store.cache import KeyValueCache

MIN_TEXT_CHARS_PER_PAGE = 200  # below this, a page is considered a scan
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_TEXT_CHARS = 2_000_000
DEFAULT_MAX_TABLE_PAGES = 20
DEFAULT_MAX_TABLE_ROWS = 2000
DEFAULT_MAX_TABLE_CHARS = 200_000


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
    tables: tuple[PdfTable, ...] = ()
    table_pages: tuple[int, ...] = ()
    table_errors: dict[int, str] = field(default_factory=dict)
    ocr_attempts: dict[int, tuple[str, ...]] = field(default_factory=dict)
    ocr_work: dict[str, int] = field(default_factory=dict)

    @cached_property
    def text(self) -> str:
        """Whole document text (text layer, completed by OCR where available), whitespace-squashed."""
        parts = [self.ocr_texts.get(i + 1) or t for i, t in enumerate(self.page_texts)]
        # Cell text is retained with its row order; no header unit or value is invented.
        return squash(" ".join(dehyphenate(p) for p in [*parts, *(table.text for table in self.tables)]))

    def coverage_summary(self) -> dict[str, int]:
        return {
            "pages": self.pages,
            "text_pages": self.pages - len(self.scanned_pages),
            "ocr_pages": len(self.ocr_texts),
            "unread_pages": len(self.unread_scanned_pages),
            "ocr_attempts": sum(map(len, self.ocr_attempts.values())),
            "ocr_errors": len(self.ocr_errors),
            "table_pages_attempted": len(self.table_pages),
            "tables": len(self.tables),
            "table_rows": sum(len(table.rows) for table in self.tables),
            "table_errors": len(self.table_errors),
            "ocr_native_pages": self.ocr_work.get("native_pages", 0),
            "ocr_cache_pages": self.ocr_work.get("cache_pages", 0),
            "ocr_requested_pages": self.ocr_work.get("requested_pages", 0),
        }

    @property
    def read_by(self) -> str:
        """Provenance summary: "text", or e.g. "text + transcript (p1) + tesseract-5.5.0:fra@300dpi (p2-3)"."""
        by_engine: dict[str, list[int]] = {}
        for page, label in sorted(self.ocr_engines.items()):
            by_engine.setdefault(label, []).append(page)
        parts = ["text"] if len(self.scanned_pages) < self.pages else []
        parts += [f"{label} (p{_ranges(pages)})" for label, pages in by_engine.items()]
        if self.tables:
            parts.append(f"tables (p{_ranges(sorted({table.page for table in self.tables}))})")
        if self.table_errors:
            parts.append(f"tables incomplete (p{_ranges(sorted(self.table_errors))})")
        if self.ocr_errors:
            parts.append(f"OCR review (p{_ranges(sorted(self.ocr_errors))})")
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
    read_tables: bool = False,
    max_table_pages: int = DEFAULT_MAX_TABLE_PAGES,
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
    max_table_chars: int = DEFAULT_MAX_TABLE_CHARS,
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
        tables: list[PdfTable] = []
        table_pages: list[int] = []
        table_errors: dict[int, str] = {}
        table_rows = table_chars = 0
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
            if read_tables:
                number = page.number + 1
                if number > max_table_pages or table_rows >= max_table_rows or table_chars >= max_table_chars:
                    table_errors[number] = "Table extraction budget exceeded"
                    continue
                table_pages.append(number)
                try:
                    found, truncated = extract_tables(
                        page, max_rows=max_table_rows - table_rows, max_chars=max_table_chars - table_chars
                    )
                except Exception as exc:
                    table_errors[number] = f"{type(exc).__name__}: table recognition failed"
                    continue
                tables.extend(found)
                table_rows += sum(len(table.rows) for table in found)
                table_chars += sum(sum(len(cell) for cell in row) for table in found for row in table.rows)
                if truncated:
                    table_errors[number] = "Table row or character budget exceeded"
        return PdfContent(
            pages=doc.page_count,
            fields=fields,
            page_texts=tuple(texts),
            scanned_pages=tuple(scanned),
            field_names=tuple(sorted(names)),
            tables=tuple(tables),
            table_pages=tuple(table_pages),
            table_errors=table_errors,
        )


def read_pdf(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    document_timeout_s: float = 30,
    read_tables: bool = False,
    max_table_pages: int = DEFAULT_MAX_TABLE_PAGES,
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
    max_table_chars: int = DEFAULT_MAX_TABLE_CHARS,
) -> PdfContent:
    """Parse untrusted bytes in an owned, terminable process with explicit resource limits."""
    if len(data) > max_bytes:
        raise ValueError("PDF exceeds byte limit")
    if min(max_table_pages, max_table_rows, max_table_chars) <= 0:
        raise ValueError("Table resource limits must be positive")
    args = (data, max_pages, max_text_chars, max_bytes, read_tables, max_table_pages, max_table_rows, max_table_chars)
    _, result = next(run_bounded(_parse_encoded, [("document", args)], max_workers=1, timeout_s=document_timeout_s))
    if result.error or result.value is None:
        raise ValueError(result.error or "PDF worker returned no content")
    return _decode(result.value)


# Bump when read_pdf's output can change (with the PyMuPDF version, it keys the reading cache).
READER_VERSION = f"2:{pymupdf.VersionBind}:{MIN_TEXT_CHARS_PER_PAGE}"


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
        read_tables: bool = False,
        max_table_pages: int = DEFAULT_MAX_TABLE_PAGES,
        max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
        max_table_chars: int = DEFAULT_MAX_TABLE_CHARS,
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
        if min(max_table_pages, max_table_rows, max_table_chars) <= 0:
            raise ValueError("Table resource limits must be positive")
        self.read_tables = read_tables
        self.max_table_pages, self.max_table_rows, self.max_table_chars = (
            max_table_pages,
            max_table_rows,
            max_table_chars,
        )
        self.version = (
            READER_VERSION
            if not read_tables
            else (f"{READER_VERSION}:tables={max_table_pages}:{max_table_rows}:{max_table_chars}")
        )
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
            if doc_id not in self._memo and self.cache.get(self.NAMESPACE, f"{doc_id}:{self.version}") is None
        ]
        workers = min(workers or self.max_workers, self.max_workers, max(1, (os.cpu_count() or 2) - 1))
        if len(missing) < PARALLEL_MIN or workers < 2:
            return 0
        completed = 0

        def store_batch(batch: list[tuple[str, bytes]]) -> int:
            jobs = [
                (
                    doc_id,
                    (
                        data,
                        self.max_pages,
                        self.max_text_chars,
                        self.max_bytes,
                        self.read_tables,
                        self.max_table_pages,
                        self.max_table_rows,
                        self.max_table_chars,
                    ),
                )
                for doc_id, data in batch
            ]
            count = 0
            for doc_id, result in run_bounded(
                _parse_encoded, jobs, max_workers=workers, timeout_s=self.document_timeout_s
            ):
                if result.error or result.value is None:
                    self.prefetch_errors[doc_id] = result.error or "PDF worker returned no content"
                    continue
                assert self.cache is not None
                self.cache.put(self.NAMESPACE, f"{doc_id}:{self.version}", result.value)
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
        key = f"{document_id}:{self.version}"
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
                read_tables=self.read_tables,
                max_table_pages=self.max_table_pages,
                max_table_rows=self.max_table_rows,
                max_table_chars=self.max_table_chars,
            )
            if self.cache is not None:
                self.cache.put(self.NAMESPACE, key, _encode(content))
        if content.pages > self.max_pages:
            raise ValueError("PDF exceeds page limit")
        if sum(map(len, content.page_texts)) > self.max_text_chars:
            raise ValueError("PDF exceeds text limit")
        size = sum(sys.getsizeof(s) for s in (*content.page_texts, *content.fields.keys(), *content.fields.values()))
        size += sys.getsizeof(content.page_texts) + sys.getsizeof(content.fields) + sys.getsizeof(content)
        size += sum(sys.getsizeof(cell) for table in content.tables for row in table.rows for cell in row)
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
    read_tables: bool = False,
    max_table_pages: int = DEFAULT_MAX_TABLE_PAGES,
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS,
    max_table_chars: int = DEFAULT_MAX_TABLE_CHARS,
) -> str:
    return _encode(
        _read_pdf(
            data,
            max_bytes=max_bytes,
            max_pages=max_pages,
            max_text_chars=max_text_chars,
            read_tables=read_tables,
            max_table_pages=max_table_pages,
            max_table_rows=max_table_rows,
            max_table_chars=max_table_chars,
        )
    )


def _encode(c: PdfContent) -> str:
    raw = {
        "pages": c.pages,
        "fields": c.fields,
        "texts": c.page_texts,
        "scanned": c.scanned_pages,
        "names": c.field_names,
        "tables": [{"page": t.page, "index": t.index, "rows": t.rows, "bbox": t.bbox} for t in c.tables],
        "table_pages": c.table_pages,
        "table_errors": c.table_errors,
    }
    return base64.b64encode(zlib.compress(json.dumps(raw, ensure_ascii=False).encode(), 6)).decode()


def _decode(value: str) -> PdfContent:
    raw = json.loads(zlib.decompress(base64.b64decode(value)))
    return PdfContent(
        pages=raw["pages"], fields=raw["fields"], page_texts=tuple(raw["texts"]),
        scanned_pages=tuple(raw["scanned"]), field_names=tuple(raw["names"]),
        tables=tuple(PdfTable(t["page"], t["index"], tuple(tuple(row) for row in t["rows"]), tuple(t["bbox"])) for t in raw.get("tables", [])),
        table_pages=tuple(raw.get("table_pages", [])),
        table_errors={int(page): error for page, error in raw.get("table_errors", {}).items()},
    )  # fmt: skip
