"""Replay of the redacted real-PDF corpus (tests/corpus): the whole chain must reproduce the reviewed snapshot,
and every versioned PDF must stay free of person data."""

import json
from pathlib import Path

import pymupdf
import pytest

from radar.domain.privacy import PersonScrubber
from radar.quality import corpus
from radar.reading.pdf import read_pdf

CORPUS = Path(__file__).parent / "corpus"
PDFS = sorted((CORPUS / "pdfs").glob("*.pdf"))


def test_replay_matches_snapshot(tmp_path: Path) -> None:
    expected = json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))
    changes = corpus.diff(expected, corpus.replay(CORPUS, tmp_path))
    assert not changes, "corpus output changed; if intended: uv run radar corpus snapshot, then review the diff\n" + (
        "\n".join(changes)
    )


@pytest.mark.parametrize("path", PDFS, ids=lambda p: p.name)
def test_corpus_pdf_holds_no_person_data(path: Path) -> None:
    content = read_pdf(path.read_bytes())
    assert not PersonScrubber().leaks(content.text + " " + " ".join(content.fields.values()))
    with pymupdf.open(path) as doc:  # type: ignore[no-untyped-call]
        assert not any(page.get_images() for page in doc)
        objects = [doc.xref_object(x, compressed=True) for x in range(1, doc.xref_length())]
        assert not any("/ByteRange" in o for o in objects), "digital signature (certificate names the signer)"
        assert not {k: v for k, v in doc.metadata.items() if v and k != "format"}, "metadata (author...) remains"


def test_manifest_lists_every_pdf() -> None:
    filings, _ = corpus.load_manifest(CORPUS)
    assert sorted(d.filename for f in filings for d in f.documents) == [p.name for p in PDFS]
