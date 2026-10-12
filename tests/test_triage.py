"""Review queue: doubt first, stakes weigh, audit items are due, reviewed filings come back when the machine changes."""

from datetime import UTC, date, datetime

import pytest

from radar.api.review import review_items, submit_review
from radar.domain.enums import Category, PowerStatus, Severity, SourceKind
from radar.domain.models import Classification, Filing, Flag, Power
from radar.pipeline.corrections import with_machine
from radar.quality.triage import triage
from radar.store.db import Database

TODAY = date(2026, 10, 12)


def filing(number: str, *flags: Flag, target: bool = False, mw: float | None = None) -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}",
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="h",
            number=number,
            title="t",
            document_ids=(),
            classification=Classification(category=Category.BESS if target else Category.OTHER, method="gold"),
            power=Power(mw_max=mw, status=PowerStatus.KNOWN) if mw else Power(),
            flags=flags,
        )
    )


def test_priority_order() -> None:
    error = Flag(code="unread_scan", severity=Severity.ERROR)
    warning = Flag(code="check_alert", severity=Severity.WARNING, field="gps", detail="ailleurs")
    audit = Flag(code="audit_sample", severity=Severity.INFO, detail="random")
    queue = triage(
        [
            filing("clean-target", target=True, mw=100),
            filing("error-other", error),
            filing("warning-target", warning, target=True, mw=47),
            filing("audit", audit),
        ],
        [],
        TODAY,
    )
    assert [i.number for i in queue] == ["warning-target", "audit", "error-other", "clean-target"]
    assert queue[0].reasons == ["contrôle automatique en alerte (gps) : ailleurs"]
    assert queue[1].reasons[0].startswith("échantillon aléatoire")


def test_review_leaves_queue_and_comes_back_when_machine_changes(loaded_db: Database) -> None:
    key = "dreal_case_by_case:hauts-de-france:010504"
    submit_review(loaded_db, key, author="Dan", changes={}, validate=True, now=datetime(2026, 10, 12, tzinfo=UTC))
    assert key not in {i.key for i in triage(loaded_db.filings(), loaded_db.corrections(), TODAY)}

    filing_now = loaded_db.filing(key)
    assert filing_now is not None and filing_now.machine is not None
    changed = filing_now.machine.model_copy(update={"values": {**filing_now.machine.values, "substation": "Autre"}})
    with loaded_db.transaction():
        loaded_db.upsert_filing(filing_now.model_copy(update={"machine": changed}))
    [item] = [i for i in triage(loaded_db.filings(), loaded_db.corrections(), TODAY) if i.key == key]
    assert item.stale_review and item.reasons[0] == "valeurs modifiées depuis la relecture : substation"


def test_cause_and_blind_are_recorded(loaded_db: Database) -> None:
    key = "dreal_case_by_case:hauts-de-france:010504"
    submit_review(loaded_db, key, author="Dan", changes={"power_mw": "120"}, cause="rule")
    submit_review(
        loaded_db, key, author="Eve", changes={"category": "bess", "power_mw": "100"}, validate=True, blind=True
    )
    rows = {(c.author, c.field): c for c in loaded_db.corrections()}
    assert rows[("Dan", "power_mw")].cause == "rule" and not rows[("Dan", "power_mw")].blind
    assert rows[("Eve", "power_mw")].blind and rows[("Eve", "validated")].blind
    with pytest.raises(ValueError, match="cause"):
        submit_review(loaded_db, key, author="Dan", changes={"power_mw": "1"}, cause="hasard")
    [item] = [i for i in review_items(loaded_db, {}) if i.key == key]
    assert {c.cause for c in item.corrections} >= {"rule"}
