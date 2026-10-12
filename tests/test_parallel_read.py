"""Parallel PDF parsing: same content as a sequential read, written to the cache; small batches stay sequential."""

from pathlib import Path

import pytest

from radar.reading import pdf
from radar.reading.pdf import PdfReader, read_pdf
from radar.store.cache import KeyValueCache

PDFS = sorted((Path(__file__).parent / "corpus" / "pdfs").glob("*.pdf"))


def test_prefetch_matches_sequential_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf, "PARALLEL_MIN", 2)
    reader = PdfReader(KeyValueCache(tmp_path / "c.sqlite"))
    docs = [(p.stem, p.read_bytes) for p in PDFS[:4]]
    assert reader.prefetch(docs, workers=2) == 4
    for p in PDFS[:4]:
        assert reader.read(p.stem, b"") == read_pdf(p.read_bytes())  # served from the cache: no bytes needed
    assert reader.prefetch(docs, workers=2) == 0  # already cached


def test_small_batches_are_not_worth_a_pool(tmp_path: Path) -> None:
    reader = PdfReader(KeyValueCache(tmp_path / "c.sqlite"))
    assert reader.prefetch([(p.stem, p.read_bytes) for p in PDFS[:3]]) == 0
    assert PdfReader().prefetch([(p.stem, p.read_bytes) for p in PDFS]) == 0  # no cache: nothing to fill
