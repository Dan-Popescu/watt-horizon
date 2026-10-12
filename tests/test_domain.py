from datetime import UTC, date, datetime

import pytest

from radar.domain.departments import code_from_name, code_from_postcode
from radar.domain.enums import Category, ChangeKind, Confidence, PowerStatus
from radar.domain.geo import haversine_km, in_france, parse_dms, parse_dms_parts
from radar.domain.ids import normalize_filing_number
from radar.domain.models import Location, Power, Project
from radar.domain.power import estimate_power, read_area_ha, read_containers, read_power, storage_duration_ok
from radar.domain.text import contains_quote, fold
from radar.pipeline.changes import track_changes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(0.85, Ha) (7403, m²) (1437, m²) (47, MW) (94, MWh) (8, m)", (47.0, 47.0, 94.0)),
        ("(199, m2) (10-20, MW) (45, m2)", (10.0, 20.0, None)),
        ("un transformateur 225 000/33 000 volts de 50 MW a 80 MW", (50.0, 80.0, None)),
        ("une puissance de 110 MW et d'une capacité de 220 MWh", (110.0, 110.0, 220.0)),
        ("système de stockage par batterie d'une puissance de 130 MWac et 260 MWh", (130.0, 130.0, 260.0)),
        ("stockage par batteries de 100 MW / 200 MWh sur la commune", (100.0, 100.0, 200.0)),
    ],
)
def test_read_power(text: str, expected: tuple[float | None, float | None, float | None]) -> None:
    reading = read_power(text)
    assert reading is not None
    assert (reading.mw_min, reading.mw_max, reading.mwh) == expected
    assert reading.quote


def test_read_power_absent_or_absurd() -> None:
    assert read_power("Poste de 2 603 m²") is None
    assert read_power("") is None
    assert read_power("une centrale de 9999 MW") is None


def test_clues_and_estimate() -> None:
    assert read_containers("Il est composé de : - 36 conteneurs batteries installés") == 36
    assert read_containers("Cela implique 20 containers qui contiennent") == 20
    assert read_area_ha("(1.2, Ha) (11500, m²)") == 1.2
    power = estimate_power(containers=30, area_ha=None)
    assert power is not None and power.status is PowerStatus.ESTIMATED
    assert power.mw_min is not None and power.mw_max is not None and power.mw_min < power.mw_max
    assert "conteneurs" in power.basis
    assert estimate_power(None, None) is None


def test_storage_duration() -> None:
    assert storage_duration_ok(47, 94) is True
    assert storage_duration_ok(100, 2000) is False
    assert storage_duration_ok(None, 94) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("49°50'58\"6", 49.84961), ("2°14'54\"2", 2.24839), ("49°13'11.8416\"N", 49.21996), ("50°59'23\"N", 50.98972)],
)
def test_parse_dms(raw: str, expected: float) -> None:
    value = parse_dms(raw)
    assert value is not None and value == pytest.approx(expected, abs=1e-4)


def test_dms_parts_and_geo() -> None:
    lon = parse_dms_parts("03", "28", "42", "E")
    assert lon == pytest.approx(3.47833, abs=1e-4)
    assert parse_dms(None) is None and parse_dms("n/a") is None
    assert in_france(49.8, 2.2) and not in_france(None, 2.2) and not in_france(10.0, 2.0)
    assert haversine_km(50.0, 2.0, 50.0, 3.0) == pytest.approx(71.5, abs=0.5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("005382 KKP", "005382"),
        ("010504/KK P", "010504"),
        ("0010504-cerfa.pdf", "010504"),
        ("010504kkp_decision.pdf", "010504"),
        ("2025-8646-decision.pdf", "2025-8646"),
        ("2025_4009.pdf", "2025-4009"),
        ("2024_8112-cerfa.pdf", "2024-8112"),
        ("mentions légales", None),
    ],
)
def test_normalize_filing_number(raw: str, expected: str | None) -> None:
    assert normalize_filing_number(raw) == expected


def test_departments() -> None:
    assert code_from_name("l’Oise") == "60"
    assert code_from_name("Pas-de-Calais") == "62"
    assert code_from_name("du Nord") == "59"
    assert code_from_name("Atlantide") is None
    assert code_from_postcode("60110") == "60"
    assert code_from_postcode("97400") == "974"
    assert code_from_postcode("20000") is None


def test_text_helpers() -> None:
    assert fold("Bonningues-lès-Calais") == "bonningues les calais"
    assert contains_quote("Le  projet consiste\nà construire", "projet consiste à construire")
    assert not contains_quote("abc", "")
    assert date(2026, 1, 1)  # keep the date import meaningful for future cases


def test_track_changes() -> None:
    t0, t1 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    base = Project(
        id="p1",
        name="Batteries — Méru",
        category=Category.BESS,
        developer="X",
        location=Location(commune="Méru"),
        power=Power(mw_max=5),
        substation=None,
        status="s",
        first_seen=None,
        updated_at=t0,
        confidence=Confidence.HIGH,
        filing_keys=("k1",),
    )
    gone = base.model_copy(update={"id": "p2"})
    same = base.model_copy(update={"updated_at": t1})  # rebuilt, nothing changed
    projects, log = track_changes([base, gone], [same], t1)
    assert projects[0].updated_at == t0 and [(c.kind, c.project_id) for c in log] == [(ChangeKind.REMOVED, "p2")]
    grown = base.model_copy(update={"power": Power(mw_max=10), "substation": "Margot"})
    _, log = track_changes([base], [grown], t1)
    assert [(f.field, f.before, f.after) for f in log[0].fields] == [
        ("power_mw", "5", "10"),
        ("substation", None, "Margot"),
    ]
    _, log = track_changes([], [base], t1)
    assert log[0].kind is ChangeKind.NEW and log[0].at == t1
