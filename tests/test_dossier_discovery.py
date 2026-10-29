"""Administrative document traversal on generated PDFs and synthetic link layouts, no live requests."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from radar.domain.enums import SourceKind
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.sources import dreal, mrae, prefectures
from radar.sources.dossier import DossierLimits, collect_dossier, dossier_number
from radar.sources.regions import get_region
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import FILLER, make_pdf
from tests.test_sources_http import FakeClock

ROOT = "https://example.test/case"


def test_query_download_neutral_annex_and_indirect_html_are_one_dossier(tmp_path: Path) -> None:
    pdf = make_pdf([FILLER])
    urls: list[str] = []
    pages = {
        "/case": '<a href="/012667-form.PDF?download=1#p1">Formulaire</a>'
        '<a href="/annexes">Pièces complémentaires</a><a href="https://else.test/a.pdf">Annexe</a>',
        "/annexes": '<a href="/contenu/telechargement/1">Annexe</a><a href="/case">Dossier</a>'
        '<a href="/012667-form.PDF?download=1">Formulaire</a>',
    }
    second = make_pdf([FILLER + " Annexe technique."])

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        path = request.url.path
        if path in pages:
            return httpx.Response(200, text=pages[path])
        return httpx.Response(200, content=second if path.startswith("/contenu/") else pdf)

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        for _ in range(2):
            result = collect_dossier(
                http,
                db,
                BlobStore(tmp_path / "blobs"),
                source=SourceKind.DREAL_CASE_BY_CASE,
                region="r",
                number="012667",
                root_url=ROOT,
                title="Stockage par batteries",
            )
            assert result.state == "complete" and result.documents == 2 and result.pages == 2
        key = "dreal_case_by_case:r:012667"
        assert len(db.documents(key)) == 2
        assert all("else.test" not in url for url in urls)
        downloaded = [edge for edge in db.dossier_edges(key) if "form.PDF" in edge["url"]]
        assert len(downloaded) == 2
        assert all(edge["state"] == "document" and edge["document_id"] for edge in downloaded)
        assert len(db.dossier_scans()) == 1


@pytest.mark.parametrize(
    "limits", [DossierLimits(depth=0), DossierLimits(pages=1), DossierLimits(documents=1), DossierLimits(bytes=10)]
)
def test_truncated_dossier_is_explicitly_partial(tmp_path: Path, limits: DossierLimits) -> None:
    pdf = make_pdf([FILLER])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/case":
            return httpx.Response(200, text='<a href="/a.pdf">Annexe</a><a href="/nested">Dossier</a>')
        if request.url.path == "/nested":
            return httpx.Response(200, text='<a href="/b.pdf">Annexe</a>')
        return httpx.Response(200, content=pdf)

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        result = collect_dossier(
            http,
            db,
            BlobStore(tmp_path / "blobs"),
            source=SourceKind.PREFECTURE,
            region="r",
            number="n",
            root_url=ROOT,
            title="Dossier",
            limits=limits,
        )
        assert result.state == "partial"
        assert db.dossier_scans()[0]["state"] == "partial"
        assert any(edge["state"] == "budget" for edge in db.dossier_edges("prefecture:r:n"))


def test_block_stops_dossier_and_keeps_graph(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return (
            httpx.Response(200, text='<a href="/a.pdf">Annexe</a><a href="/b.pdf">Annexe</a>')
            if request.url.path == "/case"
            else httpx.Response(403)
        )

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        with pytest.raises(SiteBlockedError):
            collect_dossier(
                http,
                db,
                BlobStore(tmp_path / "blobs"),
                source=SourceKind.PREFECTURE,
                region="r",
                number="n",
                root_url=ROOT,
                title="Dossier",
            )
        assert requests == ["/case", "/a.pdf"]
        assert db.dossier_scans()[0]["state"] == "blocked"


def test_anonymous_dreal_annex_is_kept_and_identity_survives_rename(tmp_path: Path) -> None:
    pdf = make_pdf([FILLER])
    region = replace(get_region("hauts-de-france"), dreal_base="https://example.test/", dreal_listings=("listing",))
    name = "annexe.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/listing":
            return httpx.Response(200, text='<a href="projet-a1.html">Projet de stockage par batteries sans numéro</a>')
        if request.url.path.endswith(".html"):
            return httpx.Response(200, text=f'<a href="{name}">Annexe</a>')
        return httpx.Response(200, content=pdf)

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        assert not dreal.collect_dreal(http, db, BlobStore(tmp_path / "blobs"), region, ("listing",), 0).errors
        key = db.filing_keys_with_documents()[0]
        assert ":dossier-" in key
        name = "012667-cerfa.pdf"
        dreal.collect_dreal(http, db, BlobStore(tmp_path / "blobs"), region, ("listing",), 0)
        assert db.filing_keys_with_documents() == [key]
        assert (
            dossier_number(
                db,
                SourceKind.DREAL_CASE_BY_CASE,
                region.slug,
                "https://example.test/projet-a1.html",
                "Nouveau titre 012667",
                "",
            )
            == key.split(":")[-1]
        )


def test_mrae_rejections_are_recorded_and_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf([FILLER])
    monkeypatch.setattr(mrae, "decide", lambda title, key: (False, "random"))
    region = replace(get_region("hauts-de-france"), mrae_pages={2026: ROOT})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/case":
            return httpx.Response(200, text='<p>Un projet non ciblé.</p><a href="/a.pdf?x=1">013143/AP</a>')
        return httpx.Response(200, content=pdf)

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        result = mrae.collect_mrae(http, db, BlobStore(tmp_path / "blobs"), region, 2026)
        assert result.audited == 1 and result.documents == 1 and result.kept == 0
        assert db.listing_counts()["audit_random"] == 1
        assert db.documents()[0].audit == "random"


def test_prefecture_solar_paths_and_neutral_documents_are_not_blanket_dropped() -> None:
    page = (
        '<a href="/Actions-de-l-Etat/Environnement/energie/photovolta-stockage">Projet hybride</a>'
        '<a href="/contenu/telechargement/12">Annexe</a>'
    )
    follow, pdfs = prefectures.parse_page(page, ROOT, "https://example.test")
    assert len(follow) == 1 and len(pdfs) == 1


def test_mrae_short_project_titles_do_not_inherit_previous_project() -> None:
    page = (
        '<p>Projet de stockage par batteries</p><a href="/a.pdf">013143/AP</a>'
        '<p>Entrepôt à Lille</p><a href="/b.pdf">013144/AP</a>'
        ' <a href="/annexe.pdf">Annexe</a>'
        '<p>Entrepôt à Lille</p><a href="/c.pdf">Dossier complet</a>'
    )
    first, second, annex, other = mrae.parse_opinions(page, ROOT)
    assert "stockage" in first.context
    assert "stockage" not in second.context
    assert annex.parent_reference == "013144/AP"
    assert other.parent_reference is None


def test_prefecture_paces_globally_across_departments_and_restores_client(tmp_path: Path) -> None:
    clock = FakeClock()
    region = replace(get_region("hauts-de-france"), prefectures={"59": "https://a.test", "62": "https://b.test"})
    with (
        Database(tmp_path / "db") as db,
        PoliteHttp(
            "t",
            default_interval_s=0,
            clock=clock,
            sleep=clock.sleep,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<h1>Environnement</h1>")),
        ) as http,
    ):
        prefectures.collect_prefectures(http, db, BlobStore(tmp_path / "blobs"), region, 1)
        assert clock.slept == [5.0]
        assert http.global_interval_s == 0


def test_prefecture_does_not_treat_storage_section_as_one_project(tmp_path: Path) -> None:
    pages = {
        prefectures.START: '<h1>Énergie et stockage</h1><a href="' + prefectures.START + '/energie/projet">Projet</a>',
        prefectures.START + "/energie/projet": "<h1>Projet de centrale de stockage par batteries</h1>"
        '<a href="/contenu/telechargement/1">Annexe</a>',
    }
    clock = FakeClock()
    with PoliteHttp(
        "t",
        default_interval_s=0,
        clock=clock,
        sleep=clock.sleep,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=pages[req.url.path])),
    ) as http:
        [candidate] = prefectures.crawl_site(http, "59", "https://example.test", 3)
        assert candidate.page_url.endswith("/energie/projet")


def test_dossier_rejects_cross_origin_redirect_before_following_it(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://else.test/file.pdf"})

    with (
        Database(tmp_path / "db") as db,
        PoliteHttp("t", default_interval_s=0, transport=httpx.MockTransport(handler)) as http,
    ):
        with pytest.raises(ValueError, match="origin"):
            collect_dossier(
                http,
                db,
                BlobStore(tmp_path / "blobs"),
                source=SourceKind.PREFECTURE,
                region="r",
                number="n",
                root_url=ROOT,
                title="Dossier",
            )
        assert requests == [ROOT]
        assert db.dossier_scans()[0]["state"] == "failed"
