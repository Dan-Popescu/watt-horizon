"""OCR engines behind one interface. Choose with `make_ocr()`; the pipeline never knows which one runs.

- TranscriptOcr: verbatim transcriptions stored as `<transcripts_dir>/<document_id>.txt` (manual or vision model).
- TesseractOcr: PyMuPDF's built-in Tesseract bridge; requires a local Tesseract install with the `fra` language.
- NullOcr: reads nothing; scanned pages are reported as unread and the filing goes to human review.
"""

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pymupdf

from radar.reading.pdf import PdfContent


class OcrEngine(Protocol):
    name: str

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        """Return `content` with `ocr_texts` filled for the scanned pages this engine could read."""
        ...


class NullOcr:
    name = "none"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        return content


class TranscriptOcr:
    """A transcription file covers the whole document; it is attached to the first scanned page."""

    name = "transcript"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        path = self.directory / f"{document_id}.txt"
        if not content.scanned_pages or not path.exists():
            return content
        first = content.scanned_pages[0]
        return replace(content, ocr_texts={first: path.read_text(encoding="utf-8")})


class TesseractOcr:
    name = "tesseract"

    def __init__(self, language: str = "fra", dpi: int = 300) -> None:
        self.language, self.dpi = language, dpi

    @staticmethod
    def available() -> bool:
        return shutil.which("tesseract") is not None

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.scanned_pages:
            return content
        texts = dict(content.ocr_texts)
        with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            for number in content.scanned_pages:
                page = doc[number - 1]
                textpage = page.get_textpage_ocr(language=self.language, dpi=self.dpi, full=True)
                texts[number] = page.get_text(textpage=textpage)
        return replace(content, ocr_texts=texts)


class ChainOcr:
    """Try engines in order; each one only fills pages still unread."""

    def __init__(self, *engines: OcrEngine) -> None:
        self.engines = engines
        self.name = "+".join(e.name for e in engines)

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        for engine in self.engines:
            if not content.unread_scanned_pages:
                break
            content = engine.complete(document_id, data, content)
        return content


def make_ocr(transcripts_dir: Path | None, use_tesseract: bool = True) -> OcrEngine:
    engines: list[OcrEngine] = []
    if transcripts_dir is not None:
        engines.append(TranscriptOcr(transcripts_dir))
    if use_tesseract and TesseractOcr.available():
        engines.append(TesseractOcr())
    return ChainOcr(*engines) if engines else NullOcr()
