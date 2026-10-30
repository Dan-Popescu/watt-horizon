"""Dense graphs remain complete while relation consumers avoid graph-sized Python copies."""

import tracemalloc
from datetime import timedelta
from pathlib import Path

import pytest

from radar.domain.models import Link, LinkDecision, Location
from radar.evaluation.campaign import current_links
from radar.linking.linker import iter_propose_links, propose_links
from radar.linking.review import iter_apply_link_decisions
from radar.pipeline.linking_state import full_link_and_build, incremental_link_and_build
from radar.store.db import Database
from radar.store.link_stream import LinkSpool
from tests.test_incremental_linking import NOW, comparable, filing, seed


def decision(a: str, b: str, verdict: str) -> LinkDecision:
    return LinkDecision(a=a, b=b, verdict=verdict, author="Synthetic", created_at=NOW)


def test_dense_iterator_is_complete_and_matches_list_contract() -> None:
    records = [filing(str(index), "Alpha", "Méru", "60000") for index in range(30)]
    expected = propose_links(records, ())
    metrics: dict[str, int] = {}
    assert list(iter_propose_links(records, (), metrics=metrics)) == expected
    assert metrics["candidate_pairs"] == len(expected) == 435


def test_dense_iterator_retains_records_but_not_all_relations() -> None:
    records = [filing(str(index), "Alpha", "Méru", "60000") for index in range(500)]
    tracemalloc.start()
    try:
        count = sum(1 for _ in iter_propose_links(records, ()))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert count == 124_750
    assert peak < 4 * 1024 * 1024


def test_decision_overlay_is_lazy_and_only_adds_missing_human_pairs() -> None:
    seen = []

    def links():
        for index in range(10):
            seen.append(index)
            yield Link(a="a", b=str(index), reasons=("Synthetic",))

    stream = iter_apply_link_decisions(
        links(), {("a", "0"): decision("a", "0", "related"), ("a", "z"): decision("a", "z", "same_project")}
    )
    assert next(stream).verdict == "related" and seen == [0]
    rest = list(stream)
    assert len(rest) == 10 and rest[-1].b == "z"


def test_spool_latest_duplicate_and_human_verdicts_preserve_all_edges() -> None:
    with LinkSpool() as links:
        links.add(
            (
                Link(a="a", b="b", reasons=("first",)),
                Link(a="a", b="b", reasons=("last",)),
                Link(a="a", b="signal", reasons=("Synthetic",)),
            )
        )
        links.apply_decisions(
            {("a", "b"): decision("a", "b", "related"), ("a", "c"): decision("a", "c", "same_project")}
        )
        assert len(links) == 3
        assert next(iter(links)).reasons == ("last",)
        assert list(links.decision_pairs(("related",))) == [("a", "b")]
        assert links.signal_keys_for(("a",), {"signal"}) == {"signal"}


def test_publication_and_incremental_retention_never_call_list_links(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Graph-sized link list was requested")

    monkeypatch.setattr(db, "links", forbidden)
    initial = incremental_link_and_build(db, NOW)
    with db.transaction():
        db.upsert_filing(filing("c", "Gamma", "Lille", "59000", 35))
    changed = incremental_link_and_build(db, NOW + timedelta(days=1))
    assert initial["links"] == changed["links"]
    assert changed["work"]["mode"] == "incremental"


def test_link_publication_error_rolls_back_links_projects_log_and_state(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db)
    incremental_link_and_build(db, NOW)
    before = comparable(db), db.link_revision(), db.project_revision(), db.get_pipeline_state("linking")
    with db.transaction():
        db.upsert_filing(filing("c", "Alpha", "Méru", "60000", 35))

    def fail(_projects):
        raise RuntimeError("Synthetic publication failure")

    monkeypatch.setattr(db, "replace_projects", fail)
    with pytest.raises(RuntimeError, match="publication failure"):
        full_link_and_build(db, NOW + timedelta(days=1))
    assert (comparable(db), db.link_revision(), db.project_revision(), db.get_pipeline_state("linking")) == before


def test_current_decisions_stream_latest_rows_without_loading_history(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db.transaction():
        db.add_link_decision(decision("a", "b", "same_project"))
        db.add_link_decision(decision("a", "b", "related"))
        db.add_link_decision(decision("a", "c", "same_project"))
        db.add_link_decision(decision("a", "c", "undecided"))

    def forbidden():
        raise AssertionError("Append-only history was materialized")

    monkeypatch.setattr(db, "link_decisions", forbidden)
    current = db.current_link_decisions()
    assert len(current) == 1 and current[("a", "b")].verdict == "related"


def test_withdrawal_applies_to_campaign_before_link_rebuild(db: Database) -> None:
    with db.transaction():
        db.replace_links((Link(a="a", b="b", reasons=("Synthetic",), verdict="same_project"),))
        db.add_link_decision(decision("a", "b", "same_project"))
    assert next(current_links(db)).verdict == "same_project"
    with db.transaction():
        db.add_link_decision(decision("a", "b", "undecided"))
    assert next(current_links(db)).verdict is None


def test_spool_cleanup_removes_temporary_database() -> None:
    with LinkSpool() as links:
        path = Path(links.conn.execute("PRAGMA database_list").fetchone()[2])
        assert path.is_file()
    assert not path.exists()


def test_missing_machine_edge_still_applies_human_cannot_link(db: Database) -> None:
    # INSEE supports grouping even when no label or distinctive developer word
    # gives the related-link proposer a reason to produce an edge.
    records = [
        filing(key, "Energy", "", "60000").model_copy(update={"location": Location(insee_code="60000")})
        for key in ("a", "b")
    ]
    with db.transaction():
        for record in records:
            db.upsert_filing(record)
    assert incremental_link_and_build(db, NOW)["projects"] == 1
    with db.transaction():
        db.add_link_decision(decision(records[0].key, records[1].key, "related"))
    report = incremental_link_and_build(db, NOW + timedelta(days=1))
    assert report["links"] == 0 and report["projects"] == 2
    assert len(list(current_links(db))) == 1  # Campaigns also see the explicit constraint.
    assert full_link_and_build(db, NOW + timedelta(days=1))["projects"] == 2
