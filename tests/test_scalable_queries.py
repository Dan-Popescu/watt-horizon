"""Large result sets keep complete totals and stable, bounded pages."""

from datetime import UTC, datetime

import pytest

from radar.api import queries
from radar.domain.enums import Category, Confidence
from radar.domain.models import Location, Power, Project
from radar.store.db import Database


def projects(count: int) -> list[Project]:
    return [
        Project(
            id=f"p:{i:04}",
            name="Synthetic",
            category=Category.BESS,
            developer=None,
            location=Location(lat=49, lon=3, commune="Méru"),
            power=Power(mw_min=1, mw_max=1),
            substation=None,
            status="Synthetic",
            first_seen=None,
            updated_at=datetime.now(UTC),
            confidence=Confidence.REVIEW,
            filing_keys=(f"f:{i}",),
        )
        for i in range(count)
    ]


def test_zone_totals_cover_more_than_the_display_limit(db: Database) -> None:
    with db.transaction():
        db.replace_projects(projects(201))
    zone = queries.zone_pressure(db, 49, 3)
    assert zone.project_count == 201
    assert zone.known_power_mw == 201
    assert zone.by_category == {"bess": 201}
    assert len(zone.projects) == 200


def test_keyset_pages_cover_ties_unknown_power_and_filter_identity(db: Database) -> None:
    rows = projects(5)
    rows[-1] = rows[-1].model_copy(update={"power": Power()})
    with db.transaction():
        db.replace_projects(rows)
    first = queries.project_page(db, limit=2)
    assert [p.project_id for p in first.items] == ["p:0000", "p:0001"]
    assert first.total == 5 and first.projects_without_power == 1
    second = queries.project_page(db, limit=2, cursor=first.next_cursor)
    third = queries.project_page(db, limit=2, cursor=second.next_cursor)
    assert [p.project_id for p in second.items + third.items] == ["p:0002", "p:0003", "p:0004"]
    assert third.next_cursor is None
    with pytest.raises(ValueError, match="Curseur"):
        queries.project_page(db, department="59", cursor=first.next_cursor)


def test_geographic_pages_use_exact_radius_before_limit_and_totals(db: Database) -> None:
    rows = projects(3)
    rows[0] = rows[0].model_copy(update={"location": Location(lat=49.01, lon=3, commune="Méru")})
    rows[1] = rows[1].model_copy(update={"location": Location(lat=50, lon=3, commune="Méru")})
    with db.transaction():
        db.replace_projects(rows)
    first = queries.project_page(db, lat=49, lon=3, radius_km=2, limit=1, commune="MÉRU")
    assert first.total == 2 and first.items[0].project_id == "p:0002"
    second = queries.project_page(db, lat=49, lon=3, radius_km=2, limit=1, commune="MÉRU", cursor=first.next_cursor)
    assert second.items[0].project_id == "p:0000" and second.next_cursor is None


def test_cursor_rejects_dataset_changes_instead_of_skipping_or_duplicating(db: Database) -> None:
    with db.transaction():
        db.replace_projects(projects(3))
    page = queries.project_page(db, limit=1)
    with db.transaction():
        db.replace_projects(projects(4))
    with pytest.raises(ValueError, match="Curseur"):
        queries.project_page(db, limit=1, cursor=page.next_cursor)
