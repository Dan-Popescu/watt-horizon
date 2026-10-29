"""Synthetic provider responses exercise discovery protocol; no live search results or human gold."""

import html
from dataclasses import replace
from typing import Any

import httpx
import pytest

from radar.domain.enums import SourceKind
from radar.domain.models import Filing, Location
from radar.net.http import PoliteHttp, RequestBudgetError
from radar.sources import press
from radar.sources.leads import resolve_leads
from radar.sources.regions import get_region
from radar.sources.search import SearchLimits, allowed_url, enqueue_plan, parse_search, plan_queries, run_search
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.store.discovery import DISCOVERY_MIGRATION, DiscoveryStore
from tests.conftest import FILLER, make_pdf

PROVIDER = "https://search.test/html/"


@pytest.fixture
def store(db: Database) -> DiscoveryStore:
    db.conn.executescript(DISCOVERY_MIGRATION)
    return DiscoveryStore(db.conn)


def plan(db: Database, *, terms: tuple[str, ...] = ("stockage batteries",)) -> Any:
    value = plan_queries(
        [], region="hauts-de-france", manual_terms=terms, provider=PROVIDER, domains=("official.test",)
    )
    enqueue_plan(db, value)
    return value


def result(url: str, title: str = "Projet stockage", snippet: str = "Pièces officielles") -> str:
    return f'<a class="result__a" href="{html.escape(url)}">{title}</a><a class="result__snippet">{snippet}</a>'


def test_query_plan_uses_known_entities_and_round_robins_kinds() -> None:
    filings = [
        Filing(
            key=f"manual:hauts-de-france:{i}",
            source=SourceKind.MANUAL,
            region="hauts-de-france",
            number=str(i),
            title="t",
            document_ids=(),
            developer=f"ALPHA {i} SAS",
            siret="82497277200027",
            location=Location(commune="Famars"),
            substation="Margot",
        )
        for i in range(20)
    ]
    value = plan_queries(
        filings, region="hauts-de-france", max_queries=5, corporate_aliases={"ALPHA VARIANT": "ALPHA 0 SAS"}
    )
    assert len(value.queries) == 5 and value.truncated
    assert {q.reason for q in value.queries} == {"company", "siret", "commune", "substation", "filing"}
    assert all(q.seed_keys for q in value.queries)
    assert any("Famars" in q.query for q in value.queries)
    with pytest.raises(ValueError, match="person/contact"):
        plan_queries([], region="hdf", manual_terms=("Monsieur Jean DUPONT",))


def test_search_campaigns_revisit_without_overwriting_previous_queries(db: Database, store: DiscoveryStore) -> None:
    first = plan_queries([], region="r", manual_terms=("stockage",), campaign="day-one")
    second = plan_queries([], region="r", manual_terms=("stockage",), campaign="day-two")
    assert enqueue_plan(db, first)["added"] == 1
    assert enqueue_plan(db, first)["added"] == 0
    assert enqueue_plan(db, second)["added"] == 1
    assert first.queries[0].key != second.queries[0].key
    assert store.status("r")["queries"] == {"pending": 2}


def test_unlocated_press_headlines_seed_official_search(db: Database, store: DiscoveryStore) -> None:
    with db.transaction():
        store.add_lead(
            region="r",
            url="https://press.test/news",
            title="ALPHA annonce un stockage par batteries",
            snippet="",
            origin="press",
            source_url="https://press.test/rss",
        )
    value = plan_queries([], region="r", lead_contexts=store.press_leads("r"))
    assert len(value.queries) == 1 and value.queries[0].reason == "press-lead"
    assert value.queries[0].seed_keys[0].startswith("lead:")
    assert "ALPHA annonce" in value.queries[0].query


def test_result_parser_unwraps_provider_and_enforces_domain_boundaries() -> None:
    page = result("//duckduckgo.com/l/?uddg=https%3A%2F%2Fofficial.test%2Fcase%3Futm_source%3Dx%26id%3D2")
    parsed = parse_search(page, PROVIDER, "q")
    assert parsed.recognized and parsed.results[0].url == "https://official.test/case?id=2"
    assert allowed_url(parsed.results[0].url, ("official.test",))
    assert not allowed_url("https://official.test.evil.example/a", ("official.test",))
    assert not allowed_url("https://user:secret@official.test/a", ("official.test",))
    assert not allowed_url("https://official.test:bad/a", ("official.test",))


def test_search_resumes_cursor_and_deduplicates_leads(db: Database, store: DiscoveryStore) -> None:
    value = plan(db)
    query = value.queries[0]
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.params.get("s") == "30":
            return httpx.Response(200, text=result("https://official.test/b"))
        return httpx.Response(
            200,
            text=result("https://official.test/a")
            + f'<form action="/html/" method="post"><input name="q" value="{html.escape(query.query)}"><input name="s" value="30"></form>',
        )

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        first = run_search(http, db, region="hauts-de-france", limits=SearchLimits(pages=1, interval_s=0))
        assert first["physical_requests"] == 1 and store.query(query.key)["state"] == "pending"
        second = run_search(http, db, region="hauts-de-france", limits=SearchLimits(pages=1, interval_s=0))
        assert second["physical_requests"] == 1 and "s=30" in urls[-1]
        assert store.query(query.key)["state"] == "complete"
        assert store.status("hauts-de-france")["leads"] == {"pending": 2}
        assert enqueue_plan(db, value)["added"] == 0
        assert run_search(http, db, region="hauts-de-france")["physical_requests"] == 0


@pytest.mark.parametrize("body,status", [("<form id='challenge-form'>captcha</form>", 200), ("blocked", 429)])
def test_search_stops_on_challenge_and_http_block(db: Database, store: DiscoveryStore, body: str, status: int) -> None:
    plan(db, terms=("batteries", "datacenter"))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, text=body)

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = run_search(http, db, region="hauts-de-france", limits=SearchLimits(interval_s=0))
    assert report["stopped"] and len(calls) == 1
    assert store.status("hauts-de-france")["queries"] == {"blocked": 1, "pending": 1}


def test_unknown_html_is_failed_not_empty_complete(db: Database, store: DiscoveryStore) -> None:
    value = plan(db)
    with PoliteHttp(
        "t",
        default_interval_s=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>Other layout</html>")),
    ) as http:
        report = run_search(http, db, region="hauts-de-france", limits=SearchLimits(interval_s=0))
    assert report["errors"] and store.query(value.queries[0].key)["state"] == "failed"


def test_physical_budget_counts_redirects_and_retries(db: Database, store: DiscoveryStore) -> None:
    query = plan(db).queries[0]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/final"})

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = run_search(http, db, region="hauts-de-france", limits=SearchLimits(requests=1, interval_s=0))
    assert calls == ["/html/"] and report["physical_requests"] == 1
    assert store.query(query.key)["requests"] == 1 and store.query(query.key)["state"] == "partial"


def test_physical_retry_admission_is_counted_before_sending(db: Database, store: DiscoveryStore) -> None:
    query = plan(db).queries[0]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503)

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = run_search(http, db, region="hauts-de-france", limits=SearchLimits(requests=2, interval_s=0))
    assert len(calls) == 2 and report["physical_requests"] == 2
    assert store.query(query.key)["requests"] == 2


def test_result_budget_retries_same_page_without_losing_tail(db: Database, store: DiscoveryStore) -> None:
    query = plan(db).queries[0]
    page = result("https://official.test/a") + result("https://official.test/b")
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
    ) as http:
        run_search(http, db, region="hauts-de-france", limits=SearchLimits(results=1, interval_s=0))
        assert store.query(query.key)["state"] == "partial"
        run_search(http, db, region="hauts-de-france", limits=SearchLimits(results=2, interval_s=0))
    assert store.status("hauts-de-france")["leads"] == {"pending": 2}
    assert store.query(query.key)["state"] == "complete"


def test_nested_request_budgets_do_not_reset_outer_limit() -> None:
    calls = []
    with (
        PoliteHttp(
            "t",
            default_interval_s=0,
            transport=httpx.MockTransport(
                lambda request: (calls.append(str(request.url)), httpx.Response(200, text="ok"))[1]
            ),
        ) as http,
        http.request_budget(2) as outer,
    ):
        with http.request_budget(2) as inner:
            http.get_bytes("https://official.test/a")
        http.get_bytes("https://official.test/b")
        with pytest.raises(RequestBudgetError):
            http.get_bytes("https://official.test/c")
    assert outer.requests == 2 and inner.requests == 1 and len(calls) == 2


def test_press_retains_unlocated_topical_lead_without_creating_signal(
    db: Database,
    store: DiscoveryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(press, "FEEDS", {"test": "https://press.test/rss"})
    xml = (
        "<rss><channel><item><title>Un nouveau stockage par batteries</title><link>https://press.test/article</link>"
        "<description>Contacter M. Jean DUPONT ou test@example.org</description></item></channel></rss>"
    )
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))
    ) as http:
        report = press.collect_press(http, db, ["Famars"], lead_store=store)
    assert report.signals == 0 and not db.signals()
    [lead] = store.leads("hauts-de-france", limit=10)
    assert lead["state"] == "pending" and lead["origin"] == "press"
    assert "DUPONT" not in lead["snippet"] and "test@example.org" not in lead["snippet"]


def test_resolve_uses_official_title_and_never_search_snippet(
    db: Database,
    store: DiscoveryStore,
    blobs: BlobStore,
) -> None:
    region = replace(get_region("hauts-de-france"), dreal_base="https://official.test/")
    with db.transaction():
        store.add_lead(
            region=region.slug,
            url="https://official.test/case",
            title="False search title 999 MW",
            snippet="Invented search facts",
            origin="search",
            source_url=PROVIDER,
        )
    pdf = make_pdf([FILLER + " Le projet prévoit 30 MW."])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return (
            httpx.Response(200, content=pdf)
            if request.url.path.endswith(".pdf")
            else httpx.Response(
                200,
                text='<title>Site administratif</title><h1>Projet de stockage par batteries</h1><a href="/012667-annexe.pdf">Annexe</a>',
            )
        )

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = resolve_leads(http, db, blobs, region=region, official_domains=("official.test",))
    assert report["resolved"] == 1 and report["physical_requests"] == 2
    assert calls == ["/case", "/012667-annexe.pdf"]
    [document] = db.documents()
    assert document.title == "Projet de stockage par batteries" and "999 MW" not in document.title
    assert store.status(region.slug)["leads"] == {"resolved": 1}


def test_national_mrae_needs_region_evidence_even_with_explicit_adapter(
    db: Database,
    store: DiscoveryStore,
    blobs: BlobStore,
) -> None:
    region = replace(get_region("hauts-de-france"), mrae_pages={2026: "https://official.test/annual"})
    with db.transaction():
        store.add_lead(
            region=region.slug,
            url="https://official.test/other.pdf",
            title="Hauts-de-France search snippet",
            snippet="",
            origin="search",
            source_url=PROVIDER,
        )
    pdf = make_pdf([FILLER + " Projet situé en Bretagne."])
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=pdf))
    ) as http:
        report = resolve_leads(
            http, db, blobs, region=region, official_domains=("official.test",), source=SourceKind.MRAE_OPINION
        )
    assert not db.documents() and report["resolved"] == 0
    assert store.status(region.slug)["leads"] == {"unconfirmed": 1}


@pytest.mark.parametrize("annual", [False, True])
def test_national_navigation_and_annual_lists_cannot_create_a_regional_case(
    annual: bool,
    db: Database,
    store: DiscoveryStore,
    blobs: BlobStore,
) -> None:
    region = replace(get_region("hauts-de-france"), mrae_pages={2026: "https://official.test/annual"})
    url = "https://official.test/annual" if annual else "https://official.test/project"
    with db.transaction():
        store.add_lead(
            region=region.slug,
            url=url,
            title="Hauts-de-France search snippet",
            snippet="",
            origin="search",
            source_url=PROVIDER,
        )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        title = "Avis rendus en Hauts-de-France" if annual else "Projet de stockage en Bretagne"
        return httpx.Response(
            200,
            text=f'<h1>{title}</h1><nav>Hauts-de-France</nav><a href="/a.pdf">Avis A</a><a href="/b.pdf">Avis B</a>',
        )

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = resolve_leads(http, db, blobs, region=region, official_domains=("official.test",))
    assert len(calls) == 1 and not db.documents() and report["resolved"] == 0
    assert store.status(region.slug)["leads"] == {"unconfirmed": 1}


def test_interrupted_search_intent_recovers_and_is_still_budgeted(db: Database, store: DiscoveryStore) -> None:
    query = plan(db).queries[0]
    with db.transaction():
        store.query_state(query.key, "running")
        attempt = store.start_attempt("query", query.key)
        store.record_request(attempt, query.key)
    with PoliteHttp(
        "t",
        default_interval_s=0,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='<div class="no-results">Nothing</div>')
        ),
    ) as http:
        report = run_search(
            http, db, region="hauts-de-france", limits=SearchLimits(total_requests_per_query=1, interval_s=0)
        )
    assert report["physical_requests"] == 0 and store.query(query.key)["state"] == "budget"
    assert db.conn.execute("SELECT state FROM discovery_attempts WHERE id=?", (attempt,)).fetchone()[0] == "interrupted"


def test_resolve_partial_http_budget_preserves_recovered_document_and_case_identity(
    db: Database,
    store: DiscoveryStore,
    blobs: BlobStore,
) -> None:
    region = replace(get_region("hauts-de-france"), dreal_base="https://official.test/")
    with db.transaction():
        store.add_lead(
            region=region.slug,
            url="https://official.test/case",
            title="lead",
            snippet="",
            origin="search",
            source_url=PROVIDER,
        )
    pdf = make_pdf([FILLER])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/case":
            return httpx.Response(
                200, text='<h1>Projet de stockage</h1><a href="/a.pdf">Annexe A</a><a href="/b.pdf">Annexe B</a>'
            )
        return httpx.Response(200, content=pdf)

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = resolve_leads(http, db, blobs, region=region, official_domains=("official.test",), max_requests=2)
    assert report["stopped"] == "physical_request_budget" and len(calls) == 2
    [lead] = store.status(region.slug)["recent_leads"]
    assert lead["state"] == "partial" and lead["documents"] == 1 and lead["filing_key"]
    assert db.dossier_scans(lead["filing_key"])[0]["state"] == "partial"
