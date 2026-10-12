"""OCR quality of an engine against verified transcripts (`<document_id>.txt`), on scanned pages only.

- word_miss_rate (primary): share of reference words the OCR misread or missed, order-free. Our transcripts are
  excerpts assembled from several passages (title, clauses), so neither order nor extra OCR words may count.
- word_error_rate: classic WER (edit distance / reference words), for complete page transcripts.
Words are folded first (case, accents, punctuation). 0 is perfect. Run where the engine exists (Docker image).
"""

from collections import Counter
from pathlib import Path

from radar.domain.text import fold
from radar.reading.ocr import OcrEngine
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore


def word_miss_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = Counter(fold(reference).split()), Counter(fold(hypothesis).split())
    total = sum(ref.values())
    return sum((ref - hyp).values()) / total if total else 0.0


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = fold(reference).split(), fold(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, start=1):
        current = [i]
        for j, other in enumerate(hyp, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1] / len(ref)


def evaluate_ocr(engine: OcrEngine, blobs: BlobStore, transcripts_dir: Path) -> dict[str, object]:
    """Word miss rate per transcribed document and the mean. Uses the engine alone, never the transcript."""
    per_document: dict[str, float] = {}
    for path in sorted(transcripts_dir.glob("*.txt")):
        document_id = path.stem
        try:
            data = blobs.get(document_id)
        except FileNotFoundError:
            continue
        content = read_pdf(data)
        if not content.scanned_pages:
            continue
        read = engine.complete(document_id, data, content)
        hypothesis = " ".join(read.ocr_texts.get(p, "") for p in content.scanned_pages)
        per_document[document_id[:12]] = round(word_miss_rate(path.read_text(encoding="utf-8"), hypothesis), 4)
    mean = round(sum(per_document.values()) / len(per_document), 4) if per_document else None
    return {
        "engine": engine.label,
        "documents": len(per_document),
        "mean_word_miss_rate": mean,
        "per_document": per_document,
    }
