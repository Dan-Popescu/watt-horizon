"""Constructed official-site layouts test qualification; no live verification is claimed."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from radar import cli
from radar.config import Settings
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources import catalog as source_catalog
from radar.sources.catalog import (
    CatalogLimitError,
    RegionConfig,
    SourceCandidate,
    adopt,
    annual_links,
    discover,
    import_regions,
    load_catalog,
    qualify,
    save_catalog,
    status,
)
from radar.sources.coverage import inventory
from radar.sources.dreal import collect_dreal, iter_articles
from radar.sources.regions import Region, get_region
from radar.store.blobs import BlobStore
from radar.store.db import Database

BASE = "https://www.hauts-de-france.developpement-durable.gouv.fr/"
MRAE = "https://www.mrae.developpement-durable.gouv.fr/"
NORMANDIE_2025 = MRAE + "avis-rendus-sur-projets-de-la-mrae-normandie-en-a1459.html"


def mrae_only_configuration(**changes: object) -> dict[str, object]:
    return {
        "slug": "normandie",
        "departments": ["14"],
        "seeds": {"mrae": [NORMANDIE_2025]},
        **changes,
    }


@pytest.mark.parametrize("base", [{}, {"dreal_base": ""}])
@pytest.mark.parametrize(
    "sources",
    [
        {"mrae_pages": {2025: NORMANDIE_2025}},
        {"seeds": {"mrae": [NORMANDIE_2025]}},
        {"prefectures": {"14": "https://www.calvados.gouv.fr"}},
    ],
)
def test_non_dreal_sources_allow_a_missing_dreal_base(base: dict[str, object], sources: dict[str, object]) -> None:
    configured = RegionConfig.model_validate({"slug": "normandie", "departments": ["14"], **sources, **base})
    assert configured.dreal_base == configured.region().dreal_base == ""
    assert configured.region().dreal_listings == ()


@pytest.mark.parametrize(
    "change",
    [
        {"dreal_listings": ["2025-r1.html"]},
        {"dreal_periods": {"2025-r1.html": "2025"}},
        {"seeds": {"dreal": [BASE], "mrae": [NORMANDIE_2025]}},
    ],
)
def test_missing_dreal_base_refuses_dreal_configuration(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"DREAL.*base"):
        RegionConfig.model_validate(mrae_only_configuration(dreal_base="", **change))


@pytest.mark.parametrize("base", [{}, {"dreal_base": ""}])
def test_missing_dreal_base_requires_another_configured_source(base: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="configured source"):
        RegionConfig.model_validate(
            {"slug": "normandie", "departments": ["14"], "seeds": {"mrae": [], "dreal": []}, **base}
        )


@pytest.mark.parametrize("base", [BASE.rstrip("/"), BASE + "?query=1", "https://example.org/", " "])
def test_nonempty_dreal_base_keeps_existing_url_validation(base: str) -> None:
    with pytest.raises(ValueError):
        RegionConfig.model_validate(mrae_only_configuration(dreal_base=base))


def test_mrae_only_import_status_discovery_adoption_and_region_lookup(tmp_path: Path) -> None:
    path, incoming = tmp_path / "catalog.json", tmp_path / "provided.json"
    incoming.write_text(json.dumps({"version": 1, "regions": [mrae_only_configuration()]}))
    assert import_regions(path, incoming)["status"] == "configured_seeds"
    assert get_region("normandie", path).dreal_base == ""
    configured = next(region for region in status(path)["regions"] if region["slug"] == "normandie")
    assert configured["dreal_base"] == "" and configured["dreal_listings"] == []
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        # Constructed connector layout around the explicitly supplied observed annual URL.
        return httpx.Response(
            200,
            text="<h1>Avis rendus sur projets de la MRAe Normandie en 2025</h1>"
            f'<a href="{NORMANDIE_2025}">Avis rendus sur projets 2025</a>'
            '<a href="opinion.pdf">012345/AP</a>',
        )

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        result = discover(http, path, "normandie", "mrae", years=(2025,), max_pages=1)
        assert result["status"] == "finished" and result["new_candidates"] == 1
        [candidate] = load_catalog(path).candidates
        assert adopt(http, path, candidate.id)["candidate"]["state"] == "adopted"
    region = get_region("normandie", path)
    assert region.dreal_base == "" and region.dreal_listings == () and region.dreal_periods == {}
    assert region.mrae_pages == {2025: NORMANDIE_2025}
    assert requests == [NORMANDIE_2025, NORMANDIE_2025]
    assert get_region("hauts-de-france", path) == get_region("hauts-de-france")


@pytest.mark.parametrize(
    ("heading", "error"),
    [
        ("Avis rendus sur projets 2024 Normandie", "candidate year"),
        ("Avis rendus sur projets 2025 Hauts-de-France", "this region"),
    ],
)
def test_mrae_only_qualification_still_requires_the_region_and_year(heading: str, error: str) -> None:
    region = RegionConfig.model_validate(mrae_only_configuration())
    with pytest.raises(ValueError, match=error):
        qualify(f'<h1>{heading}</h1><a href="opinion.pdf">012345/AP</a>', NORMANDIE_2025, region, "mrae", 2025)


def test_mrae_only_dreal_discovery_refuses_before_http(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    region = RegionConfig.model_validate(mrae_only_configuration())
    save_catalog(path, source_catalog.SourceCatalog(regions=(region,)))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="")

    with (
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
        pytest.raises(ValueError, match="DREAL source is not configured"),
    ):
        discover(http, path, "normandie", "dreal")
    assert requests == [] and load_catalog(path).last_discovery == {}


def test_mrae_only_dreal_adoption_refuses_before_http(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    region = RegionConfig.model_validate(mrae_only_configuration())
    candidate = SourceCandidate(
        id=hashlib.sha256(f"dreal:normandie:2025:{NORMANDIE_2025}".encode()).hexdigest()[:24],
        region="normandie",
        source="dreal",
        year=2025,
        url=NORMANDIE_2025,
        parent_url=NORMANDIE_2025,
        link_text="2025",
        observed_at="2026-10-29T08:00:00+00:00",
        parent_sha256="a" * 64,
    )
    save_catalog(path, source_catalog.SourceCatalog(regions=(region,), candidates=(candidate,)))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            text='<h1>Examen au cas par cas 2025</h1><a href="stockage-a1.html">Examen au cas par cas</a>',
        )

    with (
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
        pytest.raises(ValueError, match="DREAL source is not configured"),
    ):
        adopt(http, path, candidate.id)
    assert requests == []
    checked = load_catalog(path).candidates[0]
    assert checked.state == "rejected" and checked.target_sha256 is None
    assert "DREAL source is not configured" in (checked.qualification_error or "")
    assert get_region("normandie", path).dreal_listings == ()


def test_mrae_only_pure_dreal_helpers_refuse_an_empty_prefix() -> None:
    region = RegionConfig.model_validate(mrae_only_configuration())
    page = '<h1>Examen au cas par cas 2025</h1><a href="stockage-a1.html">Examen au cas par cas</a>'
    with pytest.raises(ValueError, match="DREAL source is not configured"):
        qualify(page, NORMANDIE_2025, region, "dreal", 2025)
    with pytest.raises(ValueError, match="DREAL source is not configured"):
        annual_links('<a href="2025-r1.html">2025</a>', NORMANDIE_2025, region, "dreal")


@pytest.mark.parametrize("listings", [(), ("2025-r1.html",), (NORMANDIE_2025,)])
def test_dreal_collection_without_a_base_refuses_even_overridden_listings(
    tmp_path: Path, listings: tuple[str, ...]
) -> None:
    region = Region("normandie", ("14",), "", (), {2025: NORMANDIE_2025})
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="")

    with (
        Database(tmp_path / "state.sqlite") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        with pytest.raises(ValueError, match="DREAL source is not configured"):
            collect_dreal(http, db, BlobStore(tmp_path / "blobs"), region, listings, 0)
        assert db.collection_coverage() == []
        with pytest.raises(ValueError, match="DREAL source is not configured"):
            list(iter_articles(http, region, NORMANDIE_2025, 0))
    assert requests == []


@pytest.mark.parametrize("listing_args", [[], ["--listing", NORMANDIE_2025]])
def test_cli_dreal_source_override_cannot_enable_a_mrae_only_region(
    settings: Settings, db: Database, listing_args: list[str]
) -> None:
    region = RegionConfig.model_validate(mrae_only_configuration())
    save_catalog(settings.catalog_path, source_catalog.SourceCatalog(regions=(region,)))
    args = cli.build_parser().parse_args(["collect", "dreal", "--region", "normandie", *listing_args])
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="")

    with (
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
        pytest.raises(ValueError, match="DREAL source is not configured"),
    ):
        cli._collect_one("dreal", args, settings, http, db)
    assert requests == [] and db.collection_coverage() == []


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


def test_mrae_regional_breadcrumb_can_reveal_older_annual_pages(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hauts-de-france-r1.html":
            return httpx.Response(200, text='<a href="avis-projets-2024-a7.html">Avis rendus sur projets 2024</a>')
        return httpx.Response(200, text='<a href="hauts-de-france-r1.html">Hauts-de-France</a>')

    with PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http:
        result = discover(http, path, "hauts-de-france", "mrae", years=(2024,), max_pages=2)
    assert result["pages"] == 2 and result["new_candidates"] == 1
    assert load_catalog(path).candidates[0].url == MRAE + "avis-projets-2024-a7.html"


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


def candidate(url: str, label: str = "Known annual source") -> SourceCandidate:
    identity = hashlib.sha256(f"dreal:hauts-de-france:2024:{url}".encode()).hexdigest()[:24]
    return SourceCandidate(
        id=identity,
        region="hauts-de-france",
        source="dreal",
        year=2024,
        url=url,
        parent_url=BASE + "parent-" + "b" * 300 + ".html",
        link_text=label,
        observed_at="2026-10-29T08:00:00+00:00",
        parent_sha256="a" * 64,
    )


def test_oversized_catalog_write_preserves_previous_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    original = load_catalog(path)
    save_catalog(path, original)
    before = path.read_bytes()
    oversized = original.model_copy(
        update={
            "candidates": tuple(candidate(BASE + "2024-" + "a" * 300 + f"-{i}.html", "é" * 500) for i in range(4000))
        }
    )
    with pytest.raises(CatalogLimitError, match="configuration budget"):
        save_catalog(path, oversized)
    assert path.read_bytes() == before
    assert load_catalog(path) == original


def test_catalog_budget_matches_exact_serialized_utf8_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    catalog = load_catalog(path).model_copy(update={"last_discovery": {"note": "évaluation " * 30}})
    exact = json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    monkeypatch.setattr(source_catalog, "MAX_CATALOG_BYTES", len(exact))
    save_catalog(path, catalog)
    assert path.read_bytes() == exact
    monkeypatch.setattr(source_catalog, "MAX_CATALOG_BYTES", len(exact) - 1)
    with pytest.raises(CatalogLimitError):
        save_catalog(path, catalog)
    assert path.read_bytes() == exact


def test_discovery_retains_previous_candidates_and_fitting_new_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    previous = load_catalog(path).model_copy(update={"candidates": (candidate(BASE + "existing-2024.html"),)})
    save_catalog(path, previous)
    monkeypatch.setattr(source_catalog, "MAX_CATALOG_BYTES", 4000)
    page = "".join(f'<a href="2024-r{100 + i}.html">Examen au cas par cas 2024 ' + "x" * 300 + "</a>" for i in range(5))
    with PoliteHttp(
        "t", default_interval_s=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))
    ) as http:
        report = discover(http, path, "hauts-de-france", "dreal", max_pages=1)
    saved = load_catalog(path)
    assert saved.regions == previous.regions and saved.candidates[0] == previous.candidates[0]
    assert 0 < report["new_candidates"] < 5
    assert len(saved.candidates) == report["new_candidates"] + 1
    assert report["candidate_overflow"] == 5 - report["new_candidates"]
    assert report["status"] == "partial" and report["catalog_budget_reached"] and report["catalog_saved"]
    assert path.stat().st_size <= 4000 and saved.last_discovery == report


def test_discovery_preserves_original_when_even_diagnostics_do_not_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "catalog.json"
    save_catalog(path, load_catalog(path))
    before = path.read_bytes()
    monkeypatch.setattr(source_catalog, "MAX_CATALOG_BYTES", len(before))
    with PoliteHttp(
        "t",
        default_interval_s=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<a href="2024-r7.html">2024</a>')),
    ) as http:
        report = discover(http, path, "hauts-de-france", "dreal", max_pages=1)
    assert report["status"] == "partial" and not report["catalog_saved"]
    assert report["new_candidates"] == 0 and report["candidate_overflow"] == 1
    assert path.read_bytes() == before and load_catalog(path).candidates == ()


def test_oversized_import_is_rejected_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, incoming = tmp_path / "catalog.json", tmp_path / "too-big.json"
    save_catalog(path, load_catalog(path))
    before = path.read_bytes()
    monkeypatch.setattr(source_catalog, "MAX_CATALOG_BYTES", 2000)
    incoming.write_bytes(b" " * 2001)
    with pytest.raises(CatalogLimitError, match="configuration budget"):
        import_regions(path, incoming)
    assert path.read_bytes() == before
