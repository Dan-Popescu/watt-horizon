"""OCR engines behind one interface. Choose with `make_ocr()`; the pipeline never knows which one runs.

- TranscriptOcr: verbatim transcriptions stored as `<transcripts_dir>/<document_id>.txt` (manual or vision model).
- TesseractOcr: PyMuPDF's built-in Tesseract bridge; requires a local Tesseract install with the `fra` language.
- CachedOcr: memoises an expensive engine per (document hash, engine label) in the derived-results cache.
- NullOcr: reads nothing; scanned pages are reported as unread and the filing goes to human review.

Every engine fills only pages still unread and records its label in `PdfContent.ocr_engines` (provenance).
To add an engine (cloud OCR, vision model): implement `label` + `complete`, then add it to `make_ocr`.
"""

import re
import shutil
import subprocess
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Protocol

import pymupdf

from radar.reading.pdf import PdfContent
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


class TesseractOcr:
    def __init__(self, language: str = "fra", dpi: int = 300) -> None:
        self.language, self.dpi = language, dpi
        self.label = f"tesseract-{tesseract_version()}:{language}@{dpi}dpi"

    @staticmethod
    def available() -> bool:
        return tesseract_version() is not None

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.unread_scanned_pages:
            return content
        texts: dict[int, str] = {}
        with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            for number in content.unread_scanned_pages:
                page = doc[number - 1]
                textpage = page.get_textpage_ocr(language=self.language, dpi=self.dpi, full=True)
                texts[number] = page.get_text(textpage=textpage)
        return _with_pages(content, texts, self.label)


class CachedOcr:
    NAMESPACE = "ocr"

    def __init__(self, inner: OcrEngine, cache: KeyValueCache) -> None:
        self.inner, self.cache = inner, cache
        self.label = inner.label

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.unread_scanned_pages:
            return content
        key = f"{document_id}:{self.label}:{','.join(map(str, content.unread_scanned_pages))}"
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            return _with_pages(content, {int(page): text for page, text in hit.items()}, self.label)
        done = self.inner.complete(document_id, data, content)
        new = {p: t for p, t in done.ocr_texts.items() if p not in content.ocr_texts}
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


def make_ocr(transcripts_dir: Path | None, use_tesseract: bool = True, cache: KeyValueCache | None = None) -> OcrEngine:
    engines: list[OcrEngine] = []
    if transcripts_dir is not None:
        engines.append(TranscriptOcr(transcripts_dir))  # verified text first, never cached (files may be edited)
    if use_tesseract and TesseractOcr.available():
        tesseract = TesseractOcr()
        engines.append(CachedOcr(tesseract, cache) if cache else tesseract)
    return ChainOcr(*engines) if engines else NullOcr()
