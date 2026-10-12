"""Connector parsers on small HTML/JSON/XML fixtures, and the polite HTTP client with mocked transport."""

from datetime import date

import httpx
import pytest

from radar.domain.enums import Category
from radar.net.http import PoliteHttp, SiteBlockedError
from radar.reading.pdf import read_pdf
from radar.sources import dreal, georisques, mrae, press, registry
from tests.conftest import FILLER, make_pdf

BASE = "https://www.hauts-de-france.developpement-durable.gouv.fr/"


def test_dreal_listing_and_article() -> None:
    page = """<a href="formulaire-de-demande-d-examen-au-cas-par-cas-a28542.html">Formulaire de demande d’examen au
    cas par cas, relatif au projet de centrale de stockage d’énergie par batteries sur la commune de (…)</a>
    <a href="mentions-legales-a28732.html">Mentions légales du site, page très longue pour test</a>
    <a href="2026-r5508.html?debut_listearticles=256#pagination_listearticles">Dernière page</a>"""
    articles = dreal.parse_listing(page, BASE)
    assert len(articles) == 1 and articles[0].url.endswith("-a28542.html")
    assert dreal.last_offset(page) == 256
    pdfs = dreal.parse_article_pdfs('<a href="IMG/pdf/012667-cerfa.pdf">pdf</a><a href="x.html">x</a>', BASE + "a.html")
    assert pdfs == [BASE + "IMG/pdf/012667-cerfa.pdf"]


def test_mrae_opinions_keep_context() -> None:
    page = (
        "<p>Projet de centrale photovoltaïque au sol sur la commune de Nampont-Saint-Martin (80) Dossier étudié à la "
        'demande de la SAS Flexi Énergies</p><a href="IMG/pdf/009102.pdf">009102/A P pdf - 326.9 kio</a>'
    )
    [op] = mrae.parse_opinions(page, "https://www.mrae.developpement-durable.gouv.fr/x.html")
    assert op.reference == "009102/A P" and "photovoltaïque" in op.context


def test_georisques_rules() -> None:
    dc = georisques.to_signal(
        {
            "raisonSociale": "AZUR DATACENTER",
            "codeAIOT": "1",
            "commune": "Béthune",
            "rubriques": [{"numeroRubrique": "2910"}],
        },
        "62",
    )
    assert dc is not None and dc.category_hint is Category.DATACENTER
    warehouse = georisques.to_signal({"raisonSociale": "DHL Solutions", "codeNaf": "63", "codeAIOT": "2"}, "59")
    assert warehouse is None  # old NAF 63 = warehousing, not data hosting
    assert georisques.to_signal({"raisonSociale": "GAEC DE MONTTEDI", "codeAIOT": "3"}, "62") is None


def test_registry_filters() -> None:
    base = {
        "siren": "1",
        "etat_administratif": "A",
        "date_creation": "2026-07-30",
        "activite_principale": "68.20B",
        "nature_juridique": "5710",
        "siege": {"departement": "59", "libelle_commune": "MARCQ-EN-BARŒUL"},
    }
    hit = registry.to_signal({**base, "nom_complet": "HPC DATA CENTER 41"}, Category.DATACENTER, date(2024, 1, 1))
    assert hit is not None and hit.department == "59"
    band = {**base, "nom_complet": "BATTERIE FANFARE DE VIEUX-CONDE", "activite_principale": "90.01Z"}
    assert registry.to_signal(band, Category.BESS, date(2024, 1, 1)) is None
    sole_trader = {**base, "nom_complet": "AUTO-BESS", "nature_juridique": "1000"}
    assert registry.to_signal(sole_trader, Category.BESS, date(2024, 1, 1)) is None


def test_press_feed_and_communes() -> None:
    xml = """<rss><channel><item><title>Castle IT ouvre un deuxième data center à Saint-Omer (Pas-de-Calais)</title>
    <link>https://dcmag.fr/a</link><pubDate>Mon, 12 Oct 2026 08:00:00 +0200</pubDate><description>&lt;p&gt;Le
    procès à charge, contre les data centers&lt;/p&gt;</description></item></channel></rss>"""
    [item] = press.parse_feed(xml)
    assert item.published == date(2026, 10, 12)
    assert press.cited_communes(f"{item.title} {item.summary}", ["Saint-Omer", "Calais", "Contre"]) == ("Saint-Omer",)
    signal = press.to_signal(item, "DCMag", ["Saint-Omer"])
    assert signal is not None and signal.category_hint is Category.DATACENTER


def test_pdf_reader_detects_scans() -> None:
    content = read_pdf(make_pdf([FILLER, ""], {"denomination": "APAL MW"}))
    assert content.fields == {"denomination": "APAL MW"}
    assert content.scanned_pages == (2,) and not content.is_mostly_scanned


class FakeClock:
    def __init__(self) -> None:
        self.now, self.slept = 0.0, []  # type: ignore[var-annotated]

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_http_paces_per_host_and_stops_on_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "blocked.example":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if request.url.path == "/limited":
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    clock = FakeClock()
    http = PoliteHttp(
        "t", default_interval_s=1.0, transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep
    )
    assert http.get_json("https://a.example/x") == {"ok": True}
    assert http.get_json("https://a.example/y") == {"ok": True}
    assert clock.slept == [1.0]  # second call to the same host waited
    http.get_json("https://b.example/x")
    assert clock.slept == [1.0]  # other host: no wait
    with pytest.raises(SiteBlockedError):
        http.get_bytes("https://blocked.example/")
    with pytest.raises(SiteBlockedError):
        http.get_bytes("https://blocked.example/again")  # stays blocked for the run
    with pytest.raises(SiteBlockedError):
        http.get_bytes("https://c.example/limited")
