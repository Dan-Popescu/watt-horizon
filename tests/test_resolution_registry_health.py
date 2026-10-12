"""Same-project grouping, commissioning from the national register, collector health."""

from datetime import UTC, date, datetime, timedelta

from radar.domain.enums import Category, Confidence, PowerStatus, SourceKind
from radar.domain.models import Classification, Filing, Location, Power, Signal
from radar.linking.linker import propose_links
from radar.linking.registry import commissioned
from radar.linking.resolve import group_filings
from radar.pipeline.health import collector_health
from radar.pipeline.projects import build_projects
from radar.pipeline.run import link_and_build
from radar.sources.energy_registry import to_signal
from radar.store.db import Database

NOW = datetime(2026, 10, 12, tzinfo=UTC)
AUBENTON = Location(commune="Aubenton", department="02", insee_code="02031", lat=49.83, lon=4.2, precision="exact")


def filing(key: str, **kw: object) -> Filing:
    base: dict[str, object] = {
        "key": key,
        "source": SourceKind.DREAL_CASE_BY_CASE,
        "region": "hauts-de-france",
        "number": key.rsplit(":", 1)[-1],
        "title": "t",
        "document_ids": (),
        "classification": Classification(category=Category.BESS, method="gold"),
        "confidence": Confidence.HIGH,
    }
    return Filing.model_validate({**base, **kw})


def registry_row(insee: str, kw: float, day: str, commune: str = "Aubenton") -> dict[str, object]:
    return {
        "nominstallation": "Confidentiel",
        "commune": commune,
        "codeinseecommune": insee,
        "puismaxinstallee": kw,
        "energiestockable": kw * 2,
        "datemiseenservice_date": day,
        "nbinstallations": 1,
    }


def test_registry_signal_and_strict_match() -> None:
    signal = to_signal(registry_row("02031", 98_000, "2027-03-01"))
    assert signal is not None and signal.payload["power_mw"] == "98.0" and signal.department == "02"
    assert to_signal({"commune": "X"}) is None
    project = filing(
        "d:h:1", location=AUBENTON, power=Power(mw_max=100, status=PowerStatus.KNOWN), filed_on=date(2026, 1, 5)
    )
    older = to_signal(registry_row("02031", 100_000, "2020-06-01"))  # in service before the filing: another one
    small = to_signal(registry_row("02031", 1_000, "2027-01-01"))  # same commune, much smaller
    elsewhere = to_signal(registry_row("60395", 100_000, "2027-01-01", "Méru"))
    signals = [s for s in (signal, older, small, elsewhere) if s]
    assert commissioned([project], signals) == {"d:h:1": signal}
    unknown_power = filing("d:h:2", location=AUBENTON, filed_on=date(2026, 1, 5))
    assert commissioned([unknown_power], [small]) == {"d:h:2": small}  # type: ignore[list-item]


def test_grouping_is_conservative() -> None:
    a = filing("d:h:a", developer="APAL MW", location=AUBENTON, siret="82497277200027")
    b = filing("m:h:b", developer="Apal MW", location=AUBENTON)  # same developer + same commune: merged
    c = filing("p:h:c", developer="APAL SPV 3", location=AUBENTON, siret="82497277200027")  # same SIRET + commune
    # Real case: one company, one SIRET, two communes = two projects.
    bonneuil = Location(commune="Bonneuil-les-Eaux", insee_code="60088")
    d = filing("d:h:d", developer="APAL MW", location=bonneuil, siret="82497277200027")
    groups = [[f.key for f in g] for g in group_filings([a, b, c, d])]
    assert groups == [["d:h:a", "m:h:b", "p:h:c"], ["d:h:d"]]


def test_grouped_project_and_commissioned_status() -> None:
    old = filing(
        "d:h:a", developer="APAL MW", location=AUBENTON, filed_on=date(2026, 1, 5),
        power=Power(mw_max=100, mwh=200, status=PowerStatus.KNOWN), substation="Les Hoquins",
    )  # fmt: skip
    new = filing("m:h:b", developer="APAL MW", location=AUBENTON, filed_on=date(2026, 6, 1))
    installation = to_signal(registry_row("02031", 100_000, "2027-03-01"))
    assert installation is not None
    [project] = build_projects([old, new], [installation], [], NOW, commissioned={"d:h:a": installation})
    assert project.filing_keys == ("m:h:b", "d:h:a")  # newest leads
    assert (
        project.power.mw_max == 100 and project.substation == "Les Hoquins" and project.first_seen == date(2026, 1, 5)
    )
    assert project.status.startswith("En service (registre national, mise en service le 01/03/2027")
    assert installation.key in project.signal_keys


def test_linker_compares_only_pairs_with_a_filing() -> None:
    f = filing("d:h:a", location=AUBENTON)
    press = [
        Signal(key=f"article:{i}", kind="article", source=SourceKind.PRESS, title="x", url="u", communes=("Aubenton",))
        for i in range(2)
    ]
    registry = to_signal(registry_row("02031", 1_000, "2019-01-01"))
    assert registry is not None
    links = propose_links([f], [*press, registry])
    assert sorted((lk.a, lk.b) for lk in links) == [("article:0", "d:h:a"), ("article:1", "d:h:a")]


def test_pipeline_marks_commissioned_projects(loaded_db: Database) -> None:
    installation = to_signal(registry_row("02031", 100_000, "2027-03-01"))
    assert installation is not None
    with loaded_db.transaction():
        loaded_db.upsert_signal(installation)
    link_and_build(loaded_db)
    aubenton = next(p for p in loaded_db.projects() if p.location.commune == "Aubenton")
    assert aubenton.status.startswith("En service")


def run(kind: str, minutes_ago: int, finished: bool = True, **stats: object) -> dict[str, object]:
    started = NOW - timedelta(minutes=minutes_ago)
    return {"kind": kind, "started_at": started, "finished_at": started if finished else None, "stats": stats}


def test_collector_health() -> None:
    history = [
        run("collect:dreal", 10, seen=40, kept=3),
        run("collect:press", 10, seen=0),
        run("collect:press", 60 * 24, seen=0),
        run("collect:press", 60 * 48, seen=0),
        run("collect:prefectures", 10, seen=2, stopped="www.nord.gouv.fr: connection closed"),
        run("collect:mrae", 10, finished=False),
        run("collect:mrae", 60 * 24 * 5, seen=5),
        run("eval", 5),
    ]
    by_source = {h.source: h for h in collector_health(history, NOW)}  # type: ignore[arg-type]
    assert by_source["dreal"].status == "ok"
    assert by_source["press"].flags == ["empty"] and by_source["press"].status == "alert"
    assert by_source["prefectures"].flags == ["blocked"]
    assert set(by_source["mrae"].flags) == {"crashed", "stale"}
    assert "eval" not in by_source
