"""Selective local OCR retry with conservative quantity agreement and explicit work accounting."""

import re
from dataclasses import replace

from radar.domain.quantities import measurements
from radar.reading.ocr import OcrEngine
from radar.reading.pdf import PdfContent

_CRITICAL = re.compile(r"stockage|batter|datacenter|centre de donn|puissance|capacit|\bMW", re.I)


def quality(text: str) -> tuple[int, int, int]:
    """A routing heuristic only; it does not certify transcription correctness."""
    words = re.findall(r"[A-Za-zÀ-ÿ]{3,}", text)
    bad = text.count("\ufffd") + text.count("|")
    return (len(words) - 3 * bad, len(measurements(text)), len(text.strip()))


class SelectiveOcr:
    def __init__(self, primary: OcrEngine, secondary: OcrEngine, *, max_pages: int = 8) -> None:
        if max_pages <= 0:
            raise ValueError("Secondary OCR page budget must be positive")
        self.primary, self.secondary, self.max_pages = primary, secondary, max_pages
        self.label = f"selective-v1:{primary.label}+{secondary.label}:pages={max_pages}"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        done = self.primary.complete(document_id, data, content)
        done = replace(
            done,
            initial_ocr_texts=content.initial_ocr_texts or dict(done.ocr_texts),
            initial_ocr_engines=content.initial_ocr_engines or dict(done.ocr_engines),
        )
        candidates = []
        for page in done.scanned_pages:
            if done.ocr_engines.get(page, "").startswith("transcript"):
                continue  # A curated transcript is never replaced by a machine retry.
            text = done.ocr_texts.get(page, "")
            if page in done.unread_scanned_pages or quality(text)[0] < 8:
                neighbors = " ".join(
                    done.ocr_texts.get(p) or done.page_texts[p - 1]
                    for p in (page - 1, page, page + 1)
                    if 1 <= p <= done.pages
                )
                candidates.append((not bool(_CRITICAL.search(neighbors)), page))
        ordered = sorted(candidates)
        selected = [page for _, page in ordered[: self.max_pages]]
        if not selected:
            return done
        selected_set = set(selected)
        retry = replace(
            done,
            scanned_pages=tuple(selected),
            ocr_texts={p: text for p, text in done.ocr_texts.items() if p not in selected_set},
            ocr_engines={p: label for p, label in done.ocr_engines.items() if p not in selected_set},
        )
        retried = self.secondary.complete(document_id, data, retry)
        texts, engines, errors = dict(done.ocr_texts), dict(done.ocr_engines), dict(done.ocr_errors)
        for page in selected:
            old, new = texts.get(page, ""), retried.ocr_texts.get(page, "")
            if not new.strip():
                errors[page] = retried.ocr_errors.get(page, "Secondary OCR returned no usable text")
                continue
            old_values = {(m.dimension, m.basis, m.low, m.high) for m in measurements(old)}
            new_values = {(m.dimension, m.basis, m.low, m.high) for m in measurements(new)}
            if old_values and old_values != new_values:
                errors[page] = "Secondary OCR disagrees on quantities; primary retained for human review"
                continue
            if not old.strip() or quality(new) > quality(old):
                texts[page], engines[page] = new, retried.ocr_engines.get(page, self.secondary.label)
                errors.pop(page, None)
        for _, page in ordered[self.max_pages :]:
            errors.setdefault(page, "Secondary OCR page budget exceeded")
        return replace(
            done,
            ocr_texts=texts,
            ocr_engines=engines,
            ocr_errors=errors,
            ocr_attempts=retried.ocr_attempts,
            ocr_work=retried.ocr_work,
        )
