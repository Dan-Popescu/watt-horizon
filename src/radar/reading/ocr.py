"""OCR engines behind one interface. Choose with `make_ocr()`; the pipeline never knows which one runs.

- TranscriptOcr: verbatim transcriptions stored as `<transcripts_dir>/<document_id>.txt` (manual or vision model).
- TesseractOcr: PyMuPDF's built-in Tesseract bridge; requires a local Tesseract install with the `fra` language.
- CachedOcr: memoises an expensive engine per (document hash, engine label) in the derived-results cache.
- NullOcr: reads nothing; scanned pages are reported as unread and the filing goes to human review.

Every engine fills only pages still unread and records its label in `PdfContent.ocr_engines` (provenance).
To add an engine (cloud OCR, vision model): implement `label` + `complete`, then add it to `make_ocr`.
"""

import os
import re
import shutil
import subprocess
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Protocol

import pymupdf

from radar.reading.pdf import DEFAULT_MAX_BYTES, PdfContent
from radar.reading.workers import run_bounded
from radar.store.cache import KeyValueCache


class OcrEngine(Protocol):
    @property
    def label(self) -> str:
        """Engine and version, e.g. "tesseract-5.5.0:fra"; part of cache keys and provenance."""
        ...

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        """Return `content` with `ocr_texts`/`ocr_engines` filled for unread scanned pages this engine could read."""
        ...


def _with_pages(content: PdfContent, texts: dict[int, str], label: str) -> PdfContent:
    return replace(
        content,
        ocr_texts={**content.ocr_texts, **texts},
        ocr_engines={**content.ocr_engines, **dict.fromkeys(texts, label)},
        ocr_errors={page: error for page, error in content.ocr_errors.items() if page not in texts},
    )


class NullOcr:
    label = "none"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        return content


class TranscriptOcr:
    """A transcription file covers the whole document: its text goes on the first unread scanned page and the
    other scanned pages are marked as read by the same transcript (empty text, no double counting)."""

    label = "transcript"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        path = self.directory / f"{document_id}.txt"
        pages = content.unread_scanned_pages
        if not pages or not path.exists():
            return content
        texts = dict.fromkeys(pages, "") | {pages[0]: path.read_text(encoding="utf-8")}
        return _with_pages(content, texts, self.label)


@cache
def tesseract_version() -> str | None:
    if shutil.which("tesseract") is None:
        return None
    out = subprocess.run(["tesseract", "--version"], capture_output=True, text=True, check=False)
    m = re.search(r"tesseract v?(\d+\.\d+(?:\.\d+)?)", out.stdout + out.stderr)
    return m[1] if m else "unknown"


OCR_PARALLEL_MIN_PAGES = 4
OCR_MAX_WORKERS = 2
OCR_MAX_PAGES = 100
OCR_MAX_PIXELS = 40_000_000


def _ocr_page(data: bytes, number: int, language: str, dpi: int, max_pixels: int = OCR_MAX_PIXELS) -> str:
    """OCR of one page (1-based); module-level so a worker process can run it."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        page = doc[number - 1]
        if page.rect.width * page.rect.height * (dpi / 72) ** 2 > max_pixels:
            raise ValueError("OCR page exceeds rendered pixel limit")
        textpage = page.get_textpage_ocr(language=language, dpi=dpi, full=True)
        return str(page.get_text(textpage=textpage))


class TesseractOcr:
    def __init__(
        self,
        language: str = "fra",
        dpi: int = 300,
        *,
        max_workers: int = OCR_MAX_WORKERS,
        max_pages: int = OCR_MAX_PAGES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pixels: int = OCR_MAX_PIXELS,
        document_timeout_s: float = 60,
    ) -> None:
        if min(dpi, max_workers, max_pages, max_bytes, max_pixels) <= 0:
            raise ValueError("OCR resource limits must be positive")
        self.language, self.dpi = language, dpi
        self.max_workers, self.max_pages, self.max_bytes, self.max_pixels = (
            max_workers,
            max_pages,
            max_bytes,
            max_pixels,
        )
        if document_timeout_s <= 0:
            raise ValueError("OCR timeout must be positive")
        self.document_timeout_s = document_timeout_s
        self.label = f"tesseract-{tesseract_version()}:{language}@{dpi}dpi:pages={max_pages}:pixels={max_pixels}:bytes={max_bytes}:timeout={document_timeout_s:g}"

    @staticmethod
    def available() -> bool:
        return tesseract_version() is not None

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.unread_scanned_pages:
            return content
        if len(data) > self.max_bytes:
            return replace(
                content, ocr_errors=dict.fromkeys(content.unread_scanned_pages, "OCR document exceeds byte limit")
            )
        # Keep the document budget stable across cached/partial retries: it must not advance to another 100 pages.
        allowed = set(content.scanned_pages[: self.max_pages])
        pages = [p for p in content.unread_scanned_pages if p in allowed]
        errors = {p: "OCR page budget exceeded" for p in content.unread_scanned_pages if p not in allowed}
        if not pages:
            return replace(content, ocr_errors=content.ocr_errors | errors)
        workers = min(self.max_workers, max(1, (os.cpu_count() or 2) - 1), len(pages))
        texts: dict[int, str] = {}
        jobs = ((str(p), (data, p, self.language, self.dpi, self.max_pixels)) for p in pages)
        concurrency = workers if len(pages) >= OCR_PARALLEL_MIN_PAGES else 1
        for number, result in run_bounded(
            _ocr_page,
            jobs,
            max_workers=concurrency,
            timeout_s=self.document_timeout_s,
            total_timeout_s=self.document_timeout_s,
        ):
            if result.error or result.value is None:
                errors[int(number)] = result.error or "OCR worker returned no text"
            else:
                texts[int(number)] = result.value
        return _with_pages(replace(content, ocr_errors=content.ocr_errors | errors), texts, self.label)


class CachedOcr:
    NAMESPACE = "ocr"

    def __init__(self, inner: OcrEngine, cache: KeyValueCache) -> None:
        self.inner, self.cache = inner, cache
        self.label = inner.label

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.unread_scanned_pages:
            return content
        key = f"{document_id}:{self.label}:{','.join(map(str, content.unread_scanned_pages))}"
        original_pages = set(content.ocr_texts)
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            content = _with_pages(content, {int(page): text for page, text in hit.items()}, self.label)
        done = self.inner.complete(document_id, data, content)
        # Cache successes, never an OCR failure. A partial hit must retry still unread eligible pages.
        new = {p: t for p, t in done.ocr_texts.items() if p not in original_pages}
        self.cache.put(self.NAMESPACE, key, {str(p): t for p, t in new.items()})
        return done


class ChainOcr:
    """Try engines in order; each one only fills pages still unread."""

    def __init__(self, *engines: OcrEngine) -> None:
        self.engines = engines
        self.label = "+".join(e.label for e in engines)

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        for engine in self.engines:
            if not content.unread_scanned_pages:
                break
            content = engine.complete(document_id, data, content)
        return content


def make_ocr(
    transcripts_dir: Path | None,
    use_tesseract: bool = True,
    cache: KeyValueCache | None = None,
    *,
    max_workers: int = OCR_MAX_WORKERS,
    max_pages: int = OCR_MAX_PAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pixels: int = OCR_MAX_PIXELS,
    document_timeout_s: float = 60,
) -> OcrEngine:
    engines: list[OcrEngine] = []
    if transcripts_dir is not None:
        engines.append(TranscriptOcr(transcripts_dir))  # verified text first, never cached (files may be edited)
    if use_tesseract and TesseractOcr.available():
        tesseract = TesseractOcr(
            max_workers=max_workers,
            max_pages=max_pages,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            document_timeout_s=document_timeout_s,
        )
        engines.append(CachedOcr(tesseract, cache) if cache else tesseract)
    return ChainOcr(*engines) if engines else NullOcr()
