"""Whole-corpus effect of a change: dry-run verdicts against verified values, recorded changes reach the queue."""

from datetime import date

from radar.api.review import submit_review
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category
from radar.pipeline.filings import Services
from radar.pipeline.run import process_filings
from radar.quality.propagate import Aliases
from radar.quality.shadow import shadow
from radar.quality.triage import triage
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups

AUBENTON = "dreal_case_by_case:hauts-de-france:010504"


def services(blobs: BlobStore, lookups: StaticLookups, aliases: Aliases | None = None) -> Services:
    gold = GoldClassifier(
        {"010504": GoldLabel("010504", Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)},
        known_keys=["dreal_case_by_case:hauts-de-france:010504", "dreal_case_by_case:hauts-de-france:2025-4009"],
    )
    return Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups, aliases=aliases)


def _pretend_stored_power(db: Database, mw: float) -> None:
    """Simulate the output of an older version: the stored machine power differs from what the code gives now."""
    filing = db.filing(AUBENTON)
    assert filing is not None and filing.machine is not None
    machine = filing.machine.model_copy(update={"values": {**filing.machine.values, "power_mw": mw}})
    with db.transaction():
        db.upsert_filing(filing.model_copy(update={"machine": machine}))


def test_shadow_judges_changes(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    submit_review(loaded_db, AUBENTON, author="Dan", changes={}, validate=True)  # every current value verified
    process_filings(loaded_db, services(blobs, lookups), only={AUBENTON})
    _pretend_stored_power(loaded_db, 90.0)
    report = shadow(loaded_db, services(blobs, lookups), only={AUBENTON})
    assert [(c.field, c.before, c.after, c.verdict) for c in report.changes] == [("power_mw", 90.0, 100.0, "fixes")]
    renamed = Aliases({"developer": {"apal mw": "APAL RENAMED"}})  # a "code change" that breaks a verified value
    breaking = shadow(loaded_db, services(blobs, lookups, renamed), only={AUBENTON})
    assert {(c.field, c.verdict) for c in breaking.changes} >= {("developer", "breaks")}
    assert loaded_db.filing(AUBENTON).developer == "APAL MW"  # type: ignore[union-attr]  # dry run: nothing written


def test_recorded_changes_reach_the_queue(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    _pretend_stored_power(loaded_db, 90.0)
    process_filings(loaded_db, services(blobs, lookups), only={AUBENTON})
    changes = loaded_db.machine_changes()
    assert [(c["field"], c["before"], c["after"]) for c in changes[AUBENTON]] == [("power_mw", 90.0, 100.0)]
    [item] = [
        i
        for i in triage(loaded_db.filings(), loaded_db.corrections(), date(2026, 10, 12), changes)
        if i.key == AUBENTON
    ]
    assert item.reasons[0] == "modifié par une nouvelle version : power_mw : 90.0 → 100.0"
