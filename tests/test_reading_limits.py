"""Resource limits and corrupt inputs are observable at the PDF/OCR interfaces."""

from dataclasses import replace
from pathlib import Path
from time import sleep

import pytest

from radar.reading import pdf
from radar.reading.ocr import CachedOcr, TesseractOcr
from radar.reading.pdf import PdfContent, PdfReader
from radar.reading.workers import run_bounded
from radar.store.cache import KeyValueCache
from tests.conftest import make_pdf


def test_prefetch_isolates_a_corrupt_pdf_and_keeps_other_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf, "PARALLEL_MIN", 2)
    good = make_pdf(["A valid document"])
    reader = PdfReader(KeyValueCache(tmp_path / "cache.sqlite"))
    assert reader.prefetch([("one", lambda: good), ("bad", lambda: b"broken"), ("two", lambda: good)], workers=2) == 2
    assert set(reader.prefetch_errors) == {"bad"}
    assert reader.read("one", b"").pages == 1
    assert reader.read("two", b"").pages == 1


def test_reader_applies_bytes_and_page_limits_to_sequential_reads() -> None:
    data = make_pdf(["one", "two"])
    with pytest.raises(ValueError, match="byte limit"):
        PdfReader(max_bytes=1).read("oversized", data)
    with pytest.raises(ValueError, match="page limit"):
        PdfReader(max_pages=1).read("many-pages", data)


def test_ocr_does_not_render_documents_over_budget() -> None:
    data = make_pdf([""])
    content = pdf.read_pdf(data)
    done = TesseractOcr(max_bytes=1).complete("too-big", data, content)
    assert done.unread_scanned_pages == (1,)
    done = TesseractOcr(max_pixels=1).complete("too-large-image", data, content)
    assert done.unread_scanned_pages == (1,)


def test_ocr_page_budget_does_not_advance_after_partial_retry() -> None:
    data = make_pdf(["", ""])
    content = replace(pdf.read_pdf(data), ocr_texts={1: "already read"})
    done = TesseractOcr(max_pages=1).complete("retry", data, content)
    assert done.unread_scanned_pages == (2,)
    assert done.ocr_errors == {2: "OCR page budget exceeded"}


class PartialEngine:
    label = "partial-fixture"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        if not content.unread_scanned_pages:
            return content
        page = content.unread_scanned_pages[0]
        return replace(content, ocr_texts=content.ocr_texts | {page: f"page {page}"})


def test_partial_ocr_cache_retains_success_but_allows_remaining_page(tmp_path: Path) -> None:
    data = make_pdf(["", ""])
    content = pdf.read_pdf(data)
    engine = CachedOcr(PartialEngine(), KeyValueCache(tmp_path / "ocr.sqlite"))
    assert engine.complete("partial", data, content).unread_scanned_pages == (2,)
    done = engine.complete("partial", data, content)
    assert done.unread_scanned_pages == ()
    assert done.ocr_texts == {1: "page 1", 2: "page 2"}


def delayed_value(delay: float) -> str:
    sleep(delay)
    return "finished"


def test_a_native_worker_deadline_stops_one_job_and_other_jobs_continue() -> None:
    results = dict(run_bounded(delayed_value, [("slow", (3.0,)), ("fast", (0.0,))], max_workers=1, timeout_s=0.5))
    assert results["slow"].error == "TimeoutError: worker deadline exceeded"
    assert results["fast"].value == "finished"


def test_ocr_style_total_budget_also_expires_not_started_pages() -> None:
    results = dict(
        run_bounded(
            delayed_value,
            [("slow", (3.0,)), ("queued", (0.0,))],
            max_workers=1,
            timeout_s=10,
            total_timeout_s=0.5,
        )
    )
    assert results["slow"].error == "TimeoutError: worker group deadline exceeded"
    assert results["queued"].error == "TimeoutError: worker group deadline exceeded"
