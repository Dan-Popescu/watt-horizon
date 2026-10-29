"""Double annotation: sampled filings come back for a blind second review; agreement and kappa are measured."""

import json
from datetime import UTC, date, datetime, timedelta

from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Correction, Filing, MachineOutput
from radar.quality.agreement import agreement, kappa, sampled, second_review_due
from radar.quality.triage import triage

T0 = datetime(2026, 10, 12, tzinfo=UTC)
NUMBERS = [f"2026-{i}" for i in range(200)]
SAMPLED = next(n for n in NUMBERS if sampled(n))


def _filing(number: str) -> Filing:
    values = {"category": "bess", "developer": "Alpha", "commune": "Famars"}
    return Filing(
        key=f"k:{number}", source=SourceKind.DREAL_CASE_BY_CASE, region="hdf", number=number, title="", document_ids=(),
        classification=Classification(category=Category.BESS, method="gold"),
        machine=MachineOutput(classification=Classification(category=Category.BESS, method="gold"), values=values),
    )  # fmt: skip


def _validated(f: Filing, author: str, at: datetime, blind: bool = False) -> Correction:
    snapshot = json.dumps(f.machine.values if f.machine else {})
    return Correction(filing_key=f.key, field="validated", value=snapshot, author=author, created_at=at, blind=blind)


def _typed(f: Filing, author: str, name: str, value: str, at: datetime) -> Correction:
    return Correction(filing_key=f.key, field=name, value=value, author=author, created_at=at, blind=True)


def test_sampling_is_stable_and_about_ten_percent() -> None:
    share = sum(sampled(n) for n in NUMBERS) / len(NUMBERS)
    assert 0.04 < share < 0.18 and sampled(SAMPLED) == sampled(SAMPLED)


def test_sampled_filing_returns_to_the_queue_once() -> None:
    f = _filing(SAMPLED)
    first = [_validated(f, "Dan", T0)]
    assert second_review_due(f, first)
    item = next(i for i in triage([f], first, date(2026, 10, 12)) if i.key == f.key)
    assert item.second_review and item.reasons[0].startswith("double relecture")
    partial = [*first, _typed(f, "Alex", "category", "bess", T0 + timedelta(1))]
    assert second_review_due(f, partial)
    [pending] = triage([f], partial, date(2026, 10, 12))
    assert set(pending.pending_fields) == {"commune", "developer"}
    second = [
        *partial,
        _typed(f, "Alex", "developer", "Alpha", T0 + timedelta(1)),
        _typed(f, "Alex", "commune", "Famars", T0 + timedelta(1)),
    ]
    assert not second_review_due(f, second)
    assert not triage([f], second, date(2026, 10, 12))
    unsampled = _filing(next(n for n in NUMBERS if not sampled(n)))
    assert not second_review_due(unsampled, [_validated(unsampled, "Dan", T0)])


def test_agreement_compares_first_view_with_blind_second() -> None:
    f, g = _filing("2026-a"), _filing("2026-b")
    later = T0 + timedelta(days=1)
    corrections = [
        _validated(f, "Dan", T0),  # confirms the machine: bess, Alpha, Famars
        _typed(f, "Alex", "category", "bess", later),
        _typed(f, "Alex", "developer", "ALPHA", later),  # same, case aside
        _validated(g, "Dan", T0),
        Correction(filing_key=g.key, field="developer", value="Beta", author="Dan", created_at=T0),
        _typed(g, "Alex", "category", "datacenter", later),
        _typed(g, "Alex", "developer", "Gamma", later),
    ]
    a = agreement([f, g], corrections, test_numbers={"2026-b"})
    assert a.filings == 2
    assert a.by_field["developer"]["pairs"] == 2 and a.by_field["developer"]["agree"] == 1
    assert a.by_field["category"]["rate"] == 0.5
    assert a.disagreements == []  # 2026-b is in the test split: counted, never listed


def test_kappa() -> None:
    assert kappa([("bess", "bess"), ("other", "other")]) == 1.0
    assert kappa([("bess", "other"), ("other", "bess")]) == -1.0
    assert kappa([]) is None and kappa([("bess", "bess")]) is None
