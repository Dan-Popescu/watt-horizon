"""The offline diagnostic must detect dimension swaps and stay inside its corpus."""

from pathlib import Path

import pytest
from tools import compare_reading
from tools.compare_reading import associations, compare, load_documents

from radar.quality.corpus import CorpusDocument, CorpusFiling
from radar.reading.pdf import PdfContent
from radar.store.cache import KeyValueCache


def test_comparison_counts_lost_number_unit_associations_not_just_numbers() -> None:
    reference = PdfContent(1, {}, ("Puissance 30 MW, capacité 60 MWh.",), ())
    treatment = PdfContent(1, {}, ("Puissance 60 MW, capacité 30 MWh.",), ())
    results = []
    for content in (reference, treatment):
        results.append(
            {
                "readings": {"document": {"text_sha256": content.text, "fields_sha256": "same"}},
                "_literals": {"document": associations(content)},
                "_facts": {},
            }
        )
    difference = compare(*results)
    assert difference["literal_associations_removed"] == 2
    assert difference["literal_associations_added"] == 2
    assert difference["fields_changed"] == []
    # Repeated page/table literals do not become additional recovered information.
    duplicate = PdfContent(1, {}, (reference.text + " " + reference.text,), ())
    assert associations(duplicate) == associations(reference)


def test_corpus_cannot_read_a_document_outside_its_pdf_directory(tmp_path: Path) -> None:
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "private.pdf").write_bytes(b"not a corpus document")
    filing = CorpusFiling("constructed", "unclassified", [CorpusDocument("../private.pdf", "other", "", "")])
    with pytest.raises(ValueError, match="escapes"):
        load_documents(tmp_path, [filing])


@pytest.mark.parametrize("durations,expected", [((2, 6), 4), ((2, 10, 6), 6), ((9,), 9)])
def test_pass_reports_actual_median_for_even_and_odd_document_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durations: tuple[int, ...], expected: int
) -> None:
    documents = {f"{index}.pdf": str(index).encode() for index in range(len(durations))}
    filing = CorpusFiling(
        "constructed-timing",
        "unclassified",
        [CorpusDocument(name, "other", "Constructed timing fixture", "") for name in documents],
    )
    content = PdfContent(1, {}, ("Réseau public.",), ())
    monkeypatch.setattr(compare_reading.PdfReader, "read", lambda *_: content)
    ticks = [0]
    elapsed = 0
    for duration in durations:
        ticks.extend((elapsed, elapsed + duration))
        elapsed += duration
    ticks.append(elapsed)
    clock = iter(ticks)
    monkeypatch.setattr(compare_reading, "perf_counter", lambda: next(clock))
    cache = KeyValueCache(tmp_path / "cache.sqlite")
    try:
        result = compare_reading.measure_pass([filing], documents, cache, tables=False, secondary=False, use_ocr=False)
    finally:
        cache.close()
    assert result["failures"] == {}
    assert result["documents"] == len(durations)
    assert result["document_elapsed_median_s"] == expected
