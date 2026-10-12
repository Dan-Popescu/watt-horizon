"""PDF reading: form fields (most reliable) > text layer > scanned pages flagged for OCR."""

from dataclasses import dataclass, field

import pymupdf

from radar.domain.text import squash

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

    @property
    def text(self) -> str:
        """Whole document text (text layer, completed by OCR where available), whitespace-squashed."""
        parts = [self.ocr_texts.get(i + 1) or t for i, t in enumerate(self.page_texts)]
        return squash(" ".join(parts))

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
