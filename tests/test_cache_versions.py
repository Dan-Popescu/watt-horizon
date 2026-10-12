"""Caches never re-pay for the same input; provenance and versions are recorded on every filing."""

from dataclasses import replace
from pathlib import Path

from radar.classification.base import CachedClassifier, ClassificationInput
from radar.domain.enums import Category
from radar.domain.models import Classification
from radar.reading.ocr import CachedOcr, ChainOcr, TranscriptOcr
from radar.reading.pdf import PdfContent
from radar.store.cache import KeyValueCache
from radar.store.db import Database


class CountingClassifier:
    def __init__(self, name: str = "llm:test@v1") -> None:
        self.name, self.calls = name, 0

    def classify(self, item: ClassificationInput) -> Classification | None:
        self.calls += 1
        return Classification(category=Category.BESS, method=self.name, quote="batteries")


class CountingOcr:
    label = "fake-ocr-1"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        self.calls += 1
        texts = dict.fromkeys(content.unread_scanned_pages, "texte reconnu")
        return replace(
            content,
            ocr_texts={**content.ocr_texts, **texts},
            ocr_engines={**content.ocr_engines, **dict.fromkeys(texts, self.label)},
        )


def test_classifier_cache(tmp_path: Path) -> None:
    cache = KeyValueCache(tmp_path / "c.sqlite")
    inner = CountingClassifier()
    cached = CachedClassifier(inner, cache)
    item = ClassificationInput(filing_key="k", number="1", title="t", text="batteries 47 MW")
    first, second = cached.classify(item), cached.classify(item)
    assert first == second and inner.calls == 1
    other_prompt = CachedClassifier(CountingClassifier("llm:test@v2"), cache)  # new prompt version -> new call
    other_prompt.classify(item)
    assert other_prompt.inner.calls == 1  # type: ignore[attr-defined]


def test_ocr_cache_and_transcripts_win(tmp_path: Path) -> None:
    content = PdfContent(pages=3, fields={}, page_texts=("texte", "", ""), scanned_pages=(2, 3))
    (tmp_path / "transcribed.txt").write_text("transcription vérifiée", encoding="utf-8")
    engine = CountingOcr()
    chain = ChainOcr(TranscriptOcr(tmp_path), CachedOcr(engine, KeyValueCache(tmp_path / "c.sqlite")))

    transcribed = chain.complete("transcribed", b"", content)
    assert transcribed.read_by == "text + transcript (p2-3)" and engine.calls == 0  # the transcript covers all pages
    assert "transcription vérifiée" in transcribed.text and not transcribed.unread_scanned_pages

    scanned = chain.complete("scanned", b"", content)
    assert scanned.read_by == "text + fake-ocr-1 (p2-3)"
    again = chain.complete("scanned", b"", content)
    assert again.ocr_texts == scanned.ocr_texts and engine.calls == 1  # second read served from cache


def test_filing_records_versions_and_reading(loaded_db: Database) -> None:
    filing = loaded_db.filing("dreal_case_by_case:hauts-de-france:2025-4009")
    assert filing is not None
    assert set(filing.versions) == {"extraction", "classification", "ocr"}
    assert list(filing.reading.values()) == ["transcript (p1)"]  # scanned decision read from its transcript
