"""Link review: human verdicts override the automatic grouping, and measure the linker and the grouping."""

from datetime import UTC, datetime

from starlette.testclient import TestClient

from radar.api.review import export_link_decisions, import_link_decisions, link_items, submit_link_decision
from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Filing, Link, LinkDecision, Location
from radar.linking.resolve import group_filings
from radar.linking.review import HUMAN_REASON, apply_link_decisions, queue, report
from radar.pipeline.run import link_and_build
from radar.store.db import Database
from radar.web.app import build_app

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def _filing(key: str, developer: str, commune: str, siret: str | None = None) -> Filing:
    return Filing(
        key=key, source=SourceKind.DREAL_CASE_BY_CASE, region="hdf", number=key, title="", document_ids=(),
        developer=developer, siret=siret, location=Location(commune=commune),
        classification=Classification(category=Category.BESS, method="gold"),
    )  # fmt: skip


def _decision(a: str, b: str, verdict: str) -> LinkDecision:
    return LinkDecision(a=a, b=b, verdict=verdict, author="t", created_at=NOW)


def test_must_and_cannot_links_override_grouping() -> None:
    a, b, c = (
        _filing("a", "Alpha Storage", "Famars"),
        _filing("b", "Alpha Storage", "Famars"),
        _filing("c", "Beta", "Lille"),
    )
    assert [len(g) for g in group_filings([a, b, c])] == [2, 1]
    assert [len(g) for g in group_filings([a, b, c], cannot=[("a", "b")])] == [1, 1, 1]
    assert [len(g) for g in group_filings([a, b, c], must=[("a", "c")], cannot=[("a", "b")])] == [2, 1]


def test_decisions_annotate_links_and_add_missed_merges() -> None:
    links = [Link(a="a", b="b", reasons=("même commune : famars",))]
    out = apply_link_decisions(
        links, {("a", "b"): _decision("a", "b", "unrelated"), ("a", "c"): _decision("a", "c", "same_project")}
    )
    assert out[0].verdict == "unrelated"
    assert out[1].reasons == (HUMAN_REASON,) and out[1].verdict == "same_project"


def test_report_measures_linker_and_grouping() -> None:
    a, b, c = (
        _filing("a", "Alpha Storage", "Famars"),
        _filing("b", "Alpha Storage", "Famars"),
        _filing("c", "Gamma", "Famars"),
    )
    links = [
        Link(a="a", b="b", reasons=("même commune : famars", "même nom de porteur : alpha")),
        Link(a="a", b="c", reasons=("même commune : famars",)),
    ]
    decisions = {("a", "b"): _decision("a", "b", "related"), ("a", "c"): _decision("a", "c", "same_project")}
    r = report([a, b, c], links, decisions)
    assert r.by_reason["même commune"]["decided"] == 2 and r.by_reason["même commune"]["confirmed"] == 2
    assert r.grouping == {"pairs": 2, "wrong_merges": 1, "missed_merges": 1, "precision": 0.0, "recall": 0.0}
    pairs = queue([a, b, c], links)
    assert pairs[0].merged_by_machine and (pairs[0].a, pairs[0].b) == ("a", "b")


def test_decision_rebuilds_projects_and_survives_export(db: Database) -> None:
    with db.transaction():
        for f in (_filing("a", "Alpha Storage", "Famars"), _filing("b", "Alpha Storage", "Famars")):
            db.upsert_filing(f)
    assert link_and_build(db, NOW)["projects"] == 1
    submit_link_decision(db, "b", "a", verdict="related", author="Dan", now=NOW)
    assert link_and_build(db, NOW)["projects"] == 2
    assert [i.verdict for i in link_items(db)] == ["related"]
    rows = export_link_decisions(db)
    assert import_link_decisions(db, rows) == 0  # already there
    submit_link_decision(db, "a", "b", verdict="undecided", author="Dan", now=NOW)
    assert db.current_link_decisions() == {}
    assert link_and_build(db, NOW)["projects"] == 1


def test_link_endpoints_in_review_mode(db: Database) -> None:
    with db.transaction():
        for f in (_filing("a", "Alpha Storage", "Famars"), _filing("b", "Alpha Storage", "Famars")):
            db.upsert_filing(f)
    link_and_build(db, NOW)
    app = build_app(db.path, with_mcp=False, review=(lambda d, k: None, dict))
    client = TestClient(app, base_url="http://127.0.0.1")
    assert len(client.get("/api/review/links").json()) == 1
    res = client.post("/api/review/links", json={"a": "a", "b": "b", "verdict": "bogus", "author": "Dan"})
    assert res.status_code == 400
    res = client.post("/api/review/links", json={"a": "a", "b": "b", "verdict": "unrelated", "author": "Dan"})
    assert res.json() == {"a": "a", "b": "b", "verdict": "unrelated"}
