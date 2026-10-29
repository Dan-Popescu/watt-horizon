"""Matching preserves explainable links while keeping sparse workloads bounded."""

import tracemalloc
from datetime import date

from radar.domain.enums import SignalKind, SourceKind
from radar.domain.models import Filing, Location, Power, Signal
from radar.linking.linker import propose_links
from radar.linking.registry import commissioned
from radar.linking.resolve import group_filings


def filing(key: str, **values: object) -> Filing:
    return Filing.model_validate(
        {
            "key": key,
            "source": SourceKind.MANUAL,
            "region": "test",
            "number": key,
            "title": "Synthetic matching case",
            "document_ids": (),
            **values,
        }
    )


def test_sparse_matching_does_not_materialize_unrelated_pairs() -> None:
    filings = [filing(str(i)) for i in range(1000)]
    tracemalloc.start()
    try:
        assert propose_links(filings, []) == []
        assert tracemalloc.get_traced_memory()[1] < 12 * 1024 * 1024
    finally:
        tracemalloc.stop()


def test_shared_keys_keep_order_reasons_and_dense_pairs() -> None:
    filings = [
        filing(str(i), developer="ALPHA Energy", location=Location(commune="Méru"), substation="Margot")
        for i in range(4)
    ]
    signals = [
        Signal(
            key="s",
            kind=SignalKind.COMPANY,
            source=SourceKind.COMPANY_REGISTRY,
            title="Synthetic",
            url="https://example.test",
            developer="ALPHA",
            communes=("Méru",),
        )
    ]
    links = propose_links(filings, signals)
    assert [(link.a, link.b) for link in links] == [
        ("0", "1"),
        ("0", "2"),
        ("0", "3"),
        ("1", "2"),
        ("1", "3"),
        ("2", "3"),
        ("0", "s"),
        ("1", "s"),
        ("2", "s"),
        ("3", "s"),
    ]
    assert links[0].reasons == ("même commune : meru", "même nom de porteur : alpha", "même poste visé : margot")
    assert links[-1].reasons == ("même commune : meru", "même nom de porteur : alpha")


def test_registry_matching_keeps_strict_rules_and_closest_power() -> None:
    record = filing("f", location=Location(insee_code="12345"), power=Power(mw_max=10), filed_on=date(2025, 1, 1))

    def installation(key: str, insee: str, mw: str, commissioned_on: date) -> Signal:
        return Signal(
            key=key,
            kind=SignalKind.INSTALLATION,
            source=SourceKind.ENERGY_REGISTRY,
            title="Synthetic",
            url="https://example.test",
            observed_on=commissioned_on,
            payload={"insee": insee, "power_mw": mw},
        )

    signals = [
        installation("wrong-town", "54321", "10", date(2026, 1, 1)),
        installation("old", "12345", "10", date(2024, 1, 1)),
        installation("far-power", "12345", "13", date(2026, 1, 1)),
        installation("best", "12345", "11", date(2026, 1, 1)),
    ]
    assert commissioned([record], signals)["f"].key == "best"


def test_dense_grouping_preserves_every_filing_and_human_separation() -> None:
    records = [filing(str(i), developer="ALPHA", location=Location(commune="Méru")) for i in range(2000)]
    groups = group_filings(records)
    assert len(groups) == 1 and len(groups[0]) == 2000
    separated = group_filings(records[:3], cannot=[("0", "1")])
    assert len(separated) == 2
    assert not any({"0", "1"} <= {item.key for item in group} for group in separated)


def test_incremental_dense_links_include_both_sides_of_touched_nodes() -> None:
    records = [filing(str(i), developer="Alpha", location=Location(commune="Méru")) for i in range(20)]
    signals = [
        Signal(
            key="signal:one",
            kind=SignalKind.COMPANY,
            source=SourceKind.COMPANY_REGISTRY,
            title="Synthetic",
            url="https://example.test",
            developer="Alpha",
        )
    ]
    full = propose_links(records, signals)
    metrics: dict[str, int] = {}
    touched = propose_links(records, signals, only_keys={"19"}, metrics=metrics)
    assert touched == [link for link in full if "19" in {link.a, link.b}]
    assert metrics["candidate_pairs"] == 20
