"""Link publication stages streamed input without copying every serialized relation into Python."""

import sqlite3
import tracemalloc
from collections.abc import Iterator

import pytest

from radar.domain.models import Link
from radar.store.db import Database


def test_link_publication_keeps_serialization_memory_bounded(db: Database) -> None:
    count = 12_000
    tracemalloc.start()
    try:
        with db.transaction():
            db.replace_links(
                Link(a=f"filing:{i:06d}", b="signal:synthetic", reasons=("Synthetic relation " * 30,))
                for i in range(count)
            )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert db.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == count
    assert peak < 3 * 1024 * 1024


def test_stream_failure_preserves_links_and_revision(db: Database) -> None:
    original = Link(a="a", b="b", reasons=("original",))
    with db.transaction():
        db.replace_links([original])
    revision = db.link_revision()

    def broken() -> Iterator[Link]:
        yield Link(a="c", b="d", reasons=("never published",))
        raise ValueError("input interrupted")

    with pytest.raises(ValueError, match="input interrupted"), db.transaction():
        db.replace_links(broken())
    assert db.links() == [original]
    assert db.link_revision() == revision
    with db.transaction():
        db.replace_links(iter([original]))
    assert db.link_revision() == revision


def test_streamed_duplicates_keep_last_value_and_later_empty_input_removes_all(db: Database) -> None:
    first = Link(a="a", b="b", reasons=("first value",))
    last = first.model_copy(update={"reasons": ("last value",), "verdict": "related"})
    with db.transaction():
        db.replace_links(iter([first, last]))
    assert db.links() == [last]
    revision = db.link_revision()
    with db.transaction():
        db.replace_links(iter([last]))
    assert db.link_revision() == revision
    with db.transaction():
        db.replace_links(iter(()))
    assert db.links() == []
    assert db.link_revision() == revision + 1


def test_failed_publication_rolls_back_data_and_revision(db: Database) -> None:
    original = Link(a="a", b="b", reasons=("original",))
    with db.transaction():
        db.replace_links([original])
    revision = db.link_revision()
    db.conn.executescript("""
        CREATE TRIGGER reject_new_link BEFORE INSERT ON links
        WHEN NEW.a='c'
        BEGIN SELECT RAISE(ABORT, 'publication rejected'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError, match="publication rejected"), db.transaction():
        db.replace_links(iter([Link(a="c", b="d", reasons=("rejected",))]))
    assert db.links() == [original]
    assert db.link_revision() == revision
