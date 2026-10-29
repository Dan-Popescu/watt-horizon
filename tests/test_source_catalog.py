"""Constructed official-site layouts test qualification; no live verification is claimed."""

import json
from pathlib import Path

import httpx
import pytest

from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources.catalog import (
    RegionConfig,
    adopt,
    annual_links,
    discover,
    import_regions,
    load_catalog,
    status,
)
from radar.sources.coverage import inventory
from radar.sources.regions import get_region
from radar.store.db import Database

BASE = "https://www.hauts-de-france.developpement-durable.gouv.fr/"
MRAE = "https://www.mrae.developpement-durable.gouv.fr/"


def test_annual_links_reject_generic_future_ambiguous_and_foreign_pages(tmp_path: Path) -> None:
    region = load_catalog(tmp_path / "catalog.json").regions[0]
    page = """<a href="2024-r2025.html">2024</a>
    <a href="rapport-2024-r4.html">Rapport d'activité 2024</a>
    <a href="2024-r2025.html#top">2024</a>
    <a href="2024-2025-r9.html">Examen au cas par cas 2024 et 2025</a>
    <a href="avant-2024-r3.html">Avant 2024, examen au cas par cas</a>
    <a href="2099-r3.html">2099</a>
    <a href="https://www.autre.gouv.fr/2024-r5.html">2024</a>
    <a href="cas-par-cas-2024.pdf">Examen au cas par cas 2024</a>"""
    assert annual_links(page, BASE + "seed.html", region, "dreal") == [(2024, BASE + "2024-r2025.html", "2024")]


def test_discovery_is_pending_until_structural_adoption_and_keeps_newest_daily_lists(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/2024-r7.html":
            return httpx.Response(
                200,
                text="<h1>Examen au cas par cas 2024</h1>"
                '<a href="batteries-a22.html">Demande d’examen au cas par cas de stockage</a>',
            )
        return httpx.Response(200, text='<a href="2024-r7.html">2024</a>')

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        report = discover(http, path, "hauts-de-france", "dreal", years=(2024,), max_pages=1)
        assert report["status"] == "partial" and report["new_candidates"] == 1
        candidate = load_catalog(path).candidates[0]
        assert candidate.state == "pending" and candidate.parent_sha256
        assert "2024-r7.html" not in get_region("hauts-de-france", path).dreal_listings
        result = adopt(http, path, candidate.id)
        assert result["qualification"] == "connector_structure_checked" and not result["human_verified"]
        assert "2024-r7.html" in get_region("hauts-de-france", path).dreal_listings
        assert get_region("hauts-de-france", path).dreal_listings[:2] == ("2026-r5508.html", "2025-r5506.html")
        assert adopt(http, path, candidate.id)["candidate"]["state"] == "adopted"
    assert requests.count("/2024-r7.html") == 2


def test_mrae_wrong_region_or_generic_pdf_page_cannot_be_adopted(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    response = '<h1>Avis rendus sur projets 2024 Île-de-France</h1><a href="a.pdf">012345/AP</a>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/annual-2024.html":
            return httpx.Response(200, text=response)
        return httpx.Response(200, text='<a href="annual-2024.html">Avis rendus sur projets 2024</a>')

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        discover(http, path, "hauts-de-france", "mrae", years=(2024,), max_pages=1)
        [candidate] = load_catalog(path).candidates
        with pytest.raises(ValueError, match="this region"):
            adopt(http, path, candidate.id)
        assert load_catalog(path).candidates[0].state == "rejected"
        assert load_catalog(path).candidates[0].qualification_error
        assert 2024 not in get_region("hauts-de-france", path).mrae_pages
        response = '<h1>Avis rendus sur projets 2024 Hauts-de-France</h1><a href="a.pdf">012345/AP</a>'
        adopt(http, path, candidate.id)
        assert get_region("hauts-de-france", path).mrae_pages[2024] == MRAE + "annual-2024.html"


def test_blocked_discovery_retains_partial_candidates_and_never_retries(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(200, text='<a href="2024-r7.html">2024</a>')
        return httpx.Response(429)

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        result = discover(http, path, "hauts-de-france", "dreal", max_pages=8)
    assert result["status"] == "blocked" and len(requests) == 2
    assert len(load_catalog(path).candidates) == 1
    assert status(path)["last_discovery"]["status"] == "blocked"


def test_new_regions_are_explicit_configs_not_guessed_urls(tmp_path: Path) -> None:
    path, incoming = tmp_path / "catalog.json", tmp_path / "provided.json"
    region = {
        "slug": "region-pilote",
        "departments": ["75"],
        "dreal_base": "https://www.pilote.gouv.fr/",
        "seeds": {"dreal": ["https://www.pilote.gouv.fr/cas-par-cas.html"]},
    }
    incoming.write_text(json.dumps({"version": 1, "regions": [region]}))
    result = import_regions(path, incoming)
    assert result["status"] == "configured_seeds" and not result["live_verified"]
    configured = get_region("region-pilote", path)
    assert configured.departments == ("75",) and configured.dreal_listings == () and configured.mrae_pages == {}
    with pytest.raises(ValueError, match="Unknown region"):
        get_region("region-pilote")
    region["departments"] = ["59"]
    incoming.write_text(json.dumps({"version": 1, "regions": [region]}))
    with pytest.raises(ValueError, match="Conflicting"):
        import_regions(path, incoming)


def test_import_can_extend_discovery_seeds_without_replacing_active_sources(tmp_path: Path) -> None:
    path, incoming = tmp_path / "catalog.json", tmp_path / "provided.json"
    region = load_catalog(path).regions[0].model_dump(mode="json")
    seed = BASE + "examen-au-cas-par-cas-r1.html"
    region["seeds"] = {"dreal": [seed]}
    incoming.write_text(json.dumps({"version": 1, "regions": [region]}))
    import_regions(path, incoming)
    assert load_catalog(path).regions[0].seeds["dreal"] == (seed,)
    assert get_region("hauts-de-france", path).dreal_listings == get_region("hauts-de-france").dreal_listings


@pytest.mark.parametrize(
    "change",
    [
        {"departments": ["99"]},
        {"departments": ["59", "59"]},
        {"dreal_base": "https://example.org/"},
        {"dreal_base": "https://secret@www.pilote.gouv.fr/"},
        {"dreal_listings": ["../elsewhere"]},
        {"prefectures": {"80": "https://www.somme.gouv.fr"}},
        {"mrae_pages": {2099: MRAE + "2099.html"}},
    ],
)
def test_invalid_regional_configuration_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RegionConfig.model_validate({"slug": "pilote", "departments": ["59"], "dreal_base": BASE, **change})


def test_link_budget_is_visible_not_mistaken_for_complete_discovery(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    page = "".join(f'<a href="other-{i}.html">Other {i}</a>' for i in range(501))
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
    ) as http:
        result = discover(http, path, "hauts-de-france", "dreal", max_pages=1)
    assert result["status"] == "partial" and result["link_budget_reached"]


def test_observed_nonprefix_annual_url_keeps_explicit_coverage_year(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    target = "examen-au-cas-par-cas-r77.html"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(target):
            return httpx.Response(
                200,
                text="<h1>Examen au cas par cas 2024</h1>"
                '<a href="projet-a11.html">Demande d’examen au cas par cas de stockage</a>',
            )
        return httpx.Response(200, text=f'<a href="{target}">Examen au cas par cas 2024</a>')

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        discover(http, path, "hauts-de-france", "dreal", years=(2024,), max_pages=1)
        adopt(http, path, load_catalog(path).candidates[0].id)
    region = get_region("hauts-de-france", path)
    assert region.dreal_periods[target] == "2024"
    assert region.dreal_listings[2] == target
    with Database(tmp_path / "db") as db:
        report = inventory(db, region, [2024])
    annual = [row for row in report["scopes"] if row["source"] == "dreal" and row["scope"] == target]
    assert len(annual) == 2 and all(row["period"] == "2024" for row in annual)
    assert not any(row["source"] == "dreal" and row["state"] == "unconfigured" for row in report["scopes"])


def test_existing_source_does_not_become_a_pending_candidate(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    page = '<a href="2026-r5508.html">2026</a>'
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
    ) as http:
        report = discover(http, path, "hauts-de-france", "dreal", years=(2024,), max_pages=1)
    assert not load_catalog(path).candidates
    assert report["unmatched_years"] == [2024] and report["observed_years"] == [2026]


def test_adoption_block_is_recorded_without_activating_the_candidate(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(429)
            if request.url.path == "/2024-r7.html"
            else httpx.Response(200, text='<a href="2024-r7.html">2024</a>')
        )

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        discover(http, path, "hauts-de-france", "dreal", max_pages=1)
        candidate = load_catalog(path).candidates[0]
        with pytest.raises(SiteBlockedError):
            adopt(http, path, candidate.id)
    checked = load_catalog(path).candidates[0]
    assert checked.state == "blocked" and "429" in (checked.qualification_error or "")
    assert "2024-r7.html" not in get_region("hauts-de-france", path).dreal_listings
