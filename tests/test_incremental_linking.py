"""Incremental publication stays equivalent to a full build as records and decisions change."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from radar.domain.enums import Category, Confidence, PowerStatus, SignalKind, SourceKind
from radar.domain.models import Classification, Filing, LinkDecision, Location, Power, Signal
from radar.pipeline.linking_state import full_link_and_build, incremental_link_and_build
from radar.store.db import Database

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def filing(number: str, developer: str, commune: str, insee: str, mw: float = 10) -> Filing:
    return Filing(
        key=f"manual:test:{number}",
        source=SourceKind.MANUAL,
        region="test",
        number=number,
        title="Synthetic incremental case",
        document_ids=(),
        developer=developer,
        location=Location(commune=commune, insee_code=insee),
        power=Power(mw_max=mw, status=PowerStatus.KNOWN),
        filed_on=date(2025, 1, 1),
        classification=Classification(category=Category.BESS, method="test"),
        confidence=Confidence.REVIEW,
    )


def seed(db: Database) -> None:
    with db.transaction():
        for record in [
            filing("a", "Alpha", "Méru", "60000"),
            filing("b", "Beta", "Méru", "60000", 20),
            filing("c", "Gamma", "Lille", "59000", 30),
            filing("d", "Alpha", "Méru", "60000"),
        ]:
            db.upsert_filing(record)
        db.upsert_signal(
            Signal(
                key="company:alpha",
                kind=SignalKind.COMPANY,
                source=SourceKind.COMPANY_REGISTRY,
                title="Synthetic",
                url="https://example.test",
                developer="Alpha",
                communes=("Méru",),
            )
        )


def comparable(db: Database) -> tuple[object, ...]:
    return (
        sorted(link.model_dump_json() for link in db.links()),
        [project.model_dump_json() for project in db.projects()],
        [change.model_dump_json() for change in db.changes(since="2000-01-01", limit=10000)],
    )


def test_noop_keeps_projects_dates_and_does_no_matching(tmp_path: Path) -> None:
    with Database(tmp_path / "incremental.sqlite") as db:
        seed(db)
        initial = incremental_link_and_build(db, NOW)
        before = comparable(db)
        report = incremental_link_and_build(db, NOW + timedelta(days=1))
        assert report["work"]["mode"] == "noop"
        assert report["work"]["candidate_pairs"] == 0 and report["work"]["rebuilt_projects"] == 0
        assert report["links"] == initial["links"] and report["changes"] == {}
        assert comparable(db) == before


def test_incremental_matches_full_after_changes_deletions_and_verdict_withdrawal(tmp_path: Path) -> None:
    with Database(tmp_path / "incremental.sqlite") as actual, Database(tmp_path / "full.sqlite") as expected:
        for db in (actual, expected):
            seed(db)
        incremental_link_and_build(actual, NOW)
        full_link_and_build(expected, NOW)
        assert comparable(actual) == comparable(expected)
        for step in range(7):
            at = NOW + timedelta(days=step + 1)
            for db in (actual, expected):
                with db.transaction():
                    if step == 0:
                        db.upsert_filing(filing("a", "Alpha", "Méru", "60000", 12))
                    elif step == 1:
                        db.upsert_signal(
                            Signal(
                                key="registry:new",
                                kind=SignalKind.INSTALLATION,
                                source=SourceKind.ENERGY_REGISTRY,
                                title="Synthetic registry",
                                url="https://example.test",
                                observed_on=date(2026, 1, 1),
                                payload={"insee": "60000", "power_mw": "11"},
                            )
                        )
                    elif step in {2, 3}:
                        db.add_link_decision(
                            LinkDecision(
                                a="manual:test:a",
                                b="manual:test:c",
                                verdict="same_project" if step == 2 else "undecided",
                                author="Synthetic",
                                created_at=at,
                            )
                        )
                    elif step == 4:
                        db.upsert_signal(
                            Signal(
                                key="registry:new",
                                kind=SignalKind.INSTALLATION,
                                source=SourceKind.ENERGY_REGISTRY,
                                title="Synthetic moved registry",
                                url="https://example.test",
                                observed_on=date(2026, 1, 1),
                                payload={"insee": "59000", "power_mw": "30"},
                            )
                        )
                    elif step == 5:
                        db.conn.execute("DELETE FROM signals WHERE key='registry:new'")
                        db.conn.execute("DELETE FROM filings WHERE key='manual:test:d'")
                    else:
                        db.upsert_signal(
                            Signal(
                                key="company:alpha",
                                kind=SignalKind.COMPANY,
                                source=SourceKind.COMPANY_REGISTRY,
                                title="Synthetic changed town",
                                url="https://example.test",
                                developer="Gamma",
                                communes=("Lille",),
                            )
                        )
            report = incremental_link_and_build(actual, at)
            full = full_link_and_build(expected, at)
            assert report["changes"] == full["changes"]
            assert comparable(actual) == comparable(expected)


def test_sparse_update_reuses_unrelated_projects(tmp_path: Path) -> None:
    with Database(tmp_path / "small-change.sqlite") as db:
        seed(db)
        baseline = incremental_link_and_build(db, NOW)
        with db.transaction():
            db.upsert_filing(filing("c", "Gamma", "Lille", "59000", 35))
        report = incremental_link_and_build(db, NOW + timedelta(days=1))
        assert report["work"]["mode"] == "incremental"
        assert report["work"]["candidate_pairs"] == 0
        assert report["work"]["rebuilt_projects"] == 1
        assert report["projects"] == baseline["projects"] == 3


def test_stale_policy_or_changed_outputs_force_reference_rebuild(tmp_path: Path) -> None:
    with Database(tmp_path / "recovery.sqlite") as db:
        seed(db)
        incremental_link_and_build(db, NOW)
        state = db.get_pipeline_state("linking")
        assert state is not None
        with db.transaction():
            db.set_pipeline_state("linking", {**state, "policy": "stale-policy"})
        report = incremental_link_and_build(db, NOW + timedelta(days=1))
        assert report["work"]["mode"] == "full" and report["work"]["reason"] == "policy-changed"
        assert report["changes"] == {}
        with db.transaction():
            db.replace_links([])
        report = incremental_link_and_build(db, NOW + timedelta(days=2))
        assert report["work"]["mode"] == "full" and report["work"]["reason"] == "outputs-changed"
        assert report["links"] > 0
