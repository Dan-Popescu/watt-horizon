"""OCR checks preserve measurement associations and distinguish full transcripts from excerpts."""

import json
from dataclasses import replace
from pathlib import Path

from radar.evaluation.ocr import evaluate_ocr, measurement_miss_rate, word_miss_rate
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore
from tests.conftest import make_pdf


class FixedOcr:
    label = "synthetic"

    def complete(self, document_id: str, data: bytes, content: PdfContent) -> PdfContent:
        return replace(content, ocr_texts={content.scanned_pages[0]: "100 MW 50 MWh"})


def test_measurements_detect_permutation_that_bag_of_words_misses() -> None:
    assert word_miss_rate("50 MW 100 MWh", "100 MW 50 MWh") == 0
    assert measurement_miss_rate("50 MW 100 MWh", "100 MW 50 MWh") == 1
    assert measurement_miss_rate("500 kW 1 GWh", "0,5 MW 1000 MWh") == 0
    assert measurement_miss_rate("aucune grandeur", "50 MW") is None


def test_full_transcript_is_required_for_order_sensitive_metrics(blobs: BlobStore, tmp_path: Path) -> None:
    document = blobs.put(make_pdf([""]), ".pdf")
    (tmp_path / f"{document}.txt").write_text("50 MW 100 MWh", encoding="utf-8")
    result = evaluate_ocr(FixedOcr(), blobs, tmp_path)
    assert result["mean_word_miss_rate"] == 0
    assert result["mean_measurement_miss_rate"] == 1
    assert result["mean_word_error_rate"] is None
    assert result["mean_character_error_rate"] is None
    (tmp_path / f"{document}.meta.json").write_text(json.dumps({"complete": True}), encoding="utf-8")
    full = evaluate_ocr(FixedOcr(), blobs, tmp_path)
    assert full["mean_word_error_rate"] > 0  # type: ignore[operator]
    assert full["mean_character_error_rate"] > 0  # type: ignore[operator]


def test_unread_pages_are_reported_for_mixed_documents(blobs: BlobStore, tmp_path: Path) -> None:
    document = blobs.put(make_pdf(["", ""]), ".pdf")
    (tmp_path / f"{document}.txt").write_text("50 MW", encoding="utf-8")
    result = evaluate_ocr(FixedOcr(), blobs, tmp_path)
    assert result["unread_scanned_pages"] == 1
    assert result["incomplete_documents"] == 1
