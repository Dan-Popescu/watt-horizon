"""Privacy checks and evidence images must not bypass the document resource budgets."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pymupdf
import pytest

from radar.evaluation.privacy import PrivacyAuditIncompleteError, audit_privacy
from radar.quality import pageview
from radar.quality.pageview import PageView, PageViewBusyError, render
from radar.reading.pdf import PdfReader
from radar.reading.workers import WorkResult
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import make_pdf


class ObservedBlobs(BlobStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.byte_limits: list[int | None] = []

    def get(self, digest: str, suffix: str = ".pdf", *, max_bytes: int | None = None) -> bytes:
        self.byte_limits.append(max_bytes)
        return super().get(digest, suffix, max_bytes=max_bytes)


def test_privacy_audit_bounds_blob_reads_and_fails_closed(loaded_db: Database, blobs: BlobStore) -> None:
    observed = ObservedBlobs(blobs.root)
    with pytest.raises(PrivacyAuditIncompleteError, match="privacy audit incomplete"):
        audit_privacy(loaded_db, observed, PdfReader(max_bytes=1))
    assert observed.byte_limits == [1]


def test_evidence_render_rejects_bytes_pages_and_oversized_output() -> None:
    data = make_pdf(["proof", "another page"])
    with pytest.raises(ValueError, match="byte limit"):
        render(data, "proof", "page:1", max_bytes=1)
    with pytest.raises(ValueError, match="page limit"):
        render(data, "proof", "page:1", max_pages=1)
    with pytest.raises(ValueError, match="pixel limit"):
        render(data, "proof", "page:1", max_pixels=100)


def test_evidence_render_rejects_large_embedded_image_before_rasterization() -> None:
    with pymupdf.open() as doc:  # type: ignore[no-untyped-call]
        page = doc.new_page()
        image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32), False)  # type: ignore[no-untyped-call]
        page.insert_image(page.rect, pixmap=image)
        data = doc.tobytes()
    with pytest.raises(ValueError, match="source image pixel limit"):
        render(data, "", "page:1", max_source_image_pixels=100)


def test_evidence_render_owns_a_native_deadline() -> None:
    with pytest.raises(ValueError, match="deadline"):
        render(make_pdf(["proof"]), "proof", "page:1", document_timeout_s=0.0001)


def test_evidence_render_refuses_parallel_work_and_recovers_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    expected = PageView(b"synthetic-image", 1, 1, False, 0)

    def worker(*args: Any, **kwargs: Any) -> Iterator[tuple[str, WorkResult[PageView]]]:
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test worker not released")
        yield "evidence", WorkResult(value=expected)

    monkeypatch.setattr(pageview, "run_bounded", worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(render, b"synthetic", "", "page:1")
        try:
            assert entered.wait(timeout=2)
            with pytest.raises(PageViewBusyError):
                render(b"synthetic", "", "page:1")
        finally:
            release.set()
        assert first.result(timeout=2) == expected
    assert render(b"synthetic", "", "page:1") == expected


def test_evidence_render_releases_slot_after_worker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = PageView(b"synthetic-image", 1, 1, False, 0)
    attempts = 0

    def worker(*args: Any, **kwargs: Any) -> Iterator[tuple[str, WorkResult[PageView]]]:
        nonlocal attempts
        attempts += 1
        yield (
            "evidence",
            WorkResult(error="TimeoutError: worker deadline exceeded") if attempts == 1 else WorkResult(value=expected),
        )

    monkeypatch.setattr(pageview, "run_bounded", worker)
    with pytest.raises(ValueError, match="deadline"):
        render(b"synthetic", "", "page:1")
    assert render(b"synthetic", "", "page:1") == expected
