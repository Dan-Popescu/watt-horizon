"""Repeated publication preserves unchanged rows instead of rewriting the entire dataset."""

from radar.domain.models import Link
from radar.store.db import Database


def test_replacing_same_links_leaves_rows_untouched_and_removes_only_stale(db: Database) -> None:
    first = Link(a="a", b="b", reasons=("same commune",))
    second = Link(a="b", b="c", reasons=("same developer",))
    with db.transaction():
        db.replace_links([first, second])
    db.conn.executescript("""
        CREATE TRIGGER unchanged_link_update BEFORE UPDATE ON links
        BEGIN SELECT RAISE(ABORT, 'unchanged link rewritten'); END;
        CREATE TRIGGER unchanged_link_delete BEFORE DELETE ON links
        BEGIN SELECT RAISE(ABORT, 'unchanged link deleted'); END;
    """)
    with db.transaction():
        db.replace_links([first, second])
    assert db.links() == [first, second]
    db.conn.executescript("DROP TRIGGER unchanged_link_update; DROP TRIGGER unchanged_link_delete;")
    with db.transaction():
        db.replace_links([second])
    assert db.links() == [second]


def test_replacing_same_projects_leaves_rows_untouched(loaded_db: Database) -> None:
    projects = loaded_db.projects()
    loaded_db.conn.executescript("""
        CREATE TRIGGER unchanged_project_update BEFORE UPDATE ON projects
        BEGIN SELECT RAISE(ABORT, 'unchanged project rewritten'); END;
        CREATE TRIGGER unchanged_project_delete BEFORE DELETE ON projects
        BEGIN SELECT RAISE(ABORT, 'unchanged project deleted'); END;
    """)
    with loaded_db.transaction():
        loaded_db.replace_projects(projects)
    assert loaded_db.projects() == projects
