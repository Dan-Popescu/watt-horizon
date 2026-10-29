"""Screen terms: misses of the layer-1 screen become suggestions; adopted terms widen the screen everywhere."""

from datetime import UTC, datetime
from pathlib import Path

from radar.classification.rules import screen
from radar.classification.terms import ScreenTerms
from radar.domain.enums import Category, DocumentRole, SourceKind
from radar.domain.models import Classification, DocumentRecord, Filing, MachineOutput
from radar.quality.audit import decide
from radar.quality.screen_terms import suggest

TITLE = "Projet de stockage thermique par sels fondus sur la commune de Dunkerque"


def _filing(number: str, category: Category, vocabulary: tuple[str, ...] = (), dropped: bool = False) -> Filing:
    machine = Classification(category=Category.OTHER if dropped else category, method="rules" if dropped else "gold")
    return Filing(
        key=f"dreal:hdf:{number}",
        source=SourceKind.DREAL_CASE_BY_CASE,
        region="hdf",
        number=number,
        title="",
        document_ids=(),
        classification=Classification(category=category, method="human"),
        machine=MachineOutput(classification=machine, values={}),
        vocabulary=vocabulary,
        strata={"evaluation_partition": "dev"},
    )


def _doc(filing: Filing, title: str, audit: str | None) -> DocumentRecord:
    return DocumentRecord(
        id=filing.number, source=filing.source, url=f"https://x/{filing.number}", title=title,
        role=DocumentRole.DECISION, filing_key=filing.key, fetched_at=datetime.now(UTC), filename="d.pdf", audit=audit,
    )  # fmt: skip


def test_title_miss_becomes_a_cheap_suggestion() -> None:
    missed = _filing("2026-1", Category.BESS)
    documents = {missed.key: [_doc(missed, TITLE, "random")]}
    rejected = [("https://x/2026-1", TITLE)] + [
        (f"https://y/{i}", f"Projet de carrière à Lille {i}") for i in range(40)
    ]
    found = suggest([missed], documents, rejected)
    terms = {s.term: s for s in found}
    assert "stockage thermique" in terms and terms["stockage thermique"].admits == 0
    assert terms["stockage thermique"].rescues == ["2026-1"]
    assert "carriere" not in terms  # never in a missed title


def test_text_miss_uses_vocabulary_and_costs_against_non_targets() -> None:
    dropped = _filing("2026-2", Category.DATACENTER, ("calcul", "hyperscale", "refroidissement"), dropped=True)
    others = [_filing(f"o{i}", Category.OTHER, ("refroidissement", "voirie")) for i in range(30)]
    found = suggest([dropped, *others], {}, [])
    assert [s.term for s in found if s.scope == "text"] == ["calcul", "hyperscale"]  # not « refroidissement »


def test_adopted_term_widens_title_and_text_screens(tmp_path: Path) -> None:
    path = tmp_path / "screen_terms.json"
    book = ScreenTerms.load(path)
    assert not screen(TITLE, extra=book.pattern()).keep
    book.adopt("Stockage  THERMIQUE", "test")
    book.save(path)
    pattern = ScreenTerms.load(path).pattern()
    assert screen(TITLE, extra=pattern).keep
    assert decide(TITLE, "k", rate=0, extra=pattern) == (True, None)
    book.disable(book.terms[0].id)
    assert book.pattern() is None
