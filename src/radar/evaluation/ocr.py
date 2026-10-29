"""OCR quality of an engine against verified transcripts (`<document_id>.txt`), on scanned pages only.

- word_miss_rate (primary): share of reference words the OCR misread or missed, order-free. Our transcripts are
  excerpts assembled from several passages (title, clauses), so neither order nor extra OCR words may count.
- word_error_rate: classic WER (edit distance / reference words), for complete page transcripts.
Words are folded first (case, accents, punctuation). 0 is perfect. Run where the engine exists (Docker image).
"""

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from radar.domain.quantities import measurements
from radar.domain.text import fold
from radar.reading.ocr import OcrEngine
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore

MAX_EDIT_CELLS = 2_000_000


def word_miss_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = Counter(fold(reference).split()), Counter(fold(hypothesis).split())
    total = sum(ref.values())
    return sum((ref - hyp).values()) / total if total else 0.0


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = fold(reference).split(), fold(hypothesis).split()
    return _error_rate(ref, hyp)


def _error_rate(ref: Sequence[str], hyp: Sequence[str]) -> float:
    if len(ref) * len(hyp) > MAX_EDIT_CELLS:
        raise ValueError("complete transcript exceeds edit-distance comparison budget")
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, start=1):
        current = [i]
        for j, other in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(ref)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Order-sensitive edit distance, only meaningful for a complete transcript."""
    return _error_rate(fold(reference), fold(hypothesis))


def measurement_miss_rate(reference: str, hypothesis: str) -> float | None:
    """Missing or dimensionally incorrect reference measurements; additional excerpt text is not penalized.

    This does not claim that the right physical object was identified: full field extraction is evaluated
    separately. Keeping number, unit dimension, range and AC/DC basis together catches swapped MW/MWh values.
    """

    def values(text: str) -> Counter[tuple[str, str, float, float]]:
        return Counter((m.dimension, m.basis, round(m.low, 8), round(m.high, 8)) for m in measurements(text))

    ref, hyp = values(reference), values(hypothesis)
    total = sum(ref.values())
    return sum((ref - hyp).values()) / total if total else None


def evaluate_ocr(engine: OcrEngine, blobs: BlobStore, transcripts_dir: Path) -> dict[str, object]:
    """Word miss rate per transcribed document and the mean. Uses the engine alone, never the transcript."""
    per_document: dict[str, float] = {}
    per_measurement: dict[str, float] = {}
    full_word_errors: dict[str, float] = {}
    full_character_errors: dict[str, float] = {}
    full_metric_failures: dict[str, str] = {}
    page_errors: dict[str, dict[int, str]] = {}
    failures: dict[str, str] = {}
    unread_pages = incomplete = 0
    for path in sorted(transcripts_dir.glob("*.txt")):
        document_id = path.stem
        try:
            data = blobs.get(document_id)
        except FileNotFoundError:
            failures[document_id[:12]] = "missing source document"
            continue
        try:
            content = read_pdf(data)
            if not content.scanned_pages:
                continue
            read = engine.complete(document_id, data, content)
            hypothesis = " ".join(read.ocr_texts.get(p, "") for p in content.scanned_pages)
            reference = path.read_text(encoding="utf-8")
            metadata_path = path.with_suffix(".meta.json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            if not isinstance(metadata, dict):
                raise ValueError("transcript metadata must be an object")
        except Exception as exc:
            failures[document_id[:12]] = type(exc).__name__
            continue
        short_id = document_id[:12]
        per_document[short_id] = round(word_miss_rate(reference, hypothesis), 4)
        if (miss := measurement_miss_rate(reference, hypothesis)) is not None:
            per_measurement[short_id] = round(miss, 4)
        unread_pages += len(read.unread_scanned_pages)
        incomplete += bool(read.unread_scanned_pages)
        if read.ocr_errors:
            page_errors[short_id] = read.ocr_errors
        # Existing transcripts are excerpts. A checked sidecar must explicitly declare a complete scanned-text transcript.
        if metadata.get("complete") is True:
            try:
                full_word_errors[short_id] = round(word_error_rate(reference, hypothesis), 4)
                full_character_errors[short_id] = round(character_error_rate(reference, hypothesis), 4)
            except ValueError as exc:
                full_metric_failures[short_id] = str(exc)
    mean = round(sum(per_document.values()) / len(per_document), 4) if per_document else None
    return {
        "engine": engine.label,
        "documents": len(per_document),
        "mean_word_miss_rate": mean,
        "per_document": per_document,
        "mean_measurement_miss_rate": round(sum(per_measurement.values()) / len(per_measurement), 4)
        if per_measurement
        else None,
        "measurement_documents": len(per_measurement),
        "per_measurement": per_measurement,
        "full_transcript_documents": len(full_word_errors),
        "mean_word_error_rate": round(sum(full_word_errors.values()) / len(full_word_errors), 4)
        if full_word_errors
        else None,
        "mean_character_error_rate": round(sum(full_character_errors.values()) / len(full_character_errors), 4)
        if full_character_errors
        else None,
        "unread_scanned_pages": unread_pages,
        "incomplete_documents": incomplete,
        "failed_documents": failures,
        "page_errors": page_errors,
        "full_transcript_metric_failures": full_metric_failures,
    }
