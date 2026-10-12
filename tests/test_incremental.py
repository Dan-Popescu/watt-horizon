"""Incremental processing: unchanged filings are skipped; any change to what they depend on rebuilds them."""

from datetime import UTC, datetime

from radar.domain.models import Correction
from radar.pipeline.filings import Services
from radar.pipeline.fingerprint import code_fingerprint
from radar.pipeline.run import process_filings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups


def _services(blobs: BlobStore, lookups: StaticLookups, setup: str = "v1") -> Services:
    return Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=lookups, fingerprint=setup)


def test_unchanged_filings_are_skipped(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    first = process_filings(loaded_db, _services(blobs, lookups))
    assert first.processed == 3 and first.unchanged == 0
    versions = loaded_db.filings()[0].versions
    assert versions["code"] == code_fingerprint() and "input" in versions
    second = process_filings(loaded_db, _services(blobs, lookups))
    assert (second.processed, second.unchanged) == (0, 3)

    assert process_filings(loaded_db, _services(blobs, lookups, "v2")).processed == 3  # gold files / setup changed
    assert process_filings(loaded_db, _services(blobs, lookups, "v2"), full=True).processed == 3
    key = loaded_db.filings()[0].key
    assert process_filings(loaded_db, _services(blobs, lookups, "v2"), only={key}).processed == 1  # review rebuild

    with loaded_db.transaction():
        loaded_db.add_correction(
            Correction(
                filing_key=key, field="substation", value="Les Hoquins", author="t", created_at=datetime.now(UTC)
            )
        )
    assert process_filings(loaded_db, _services(blobs, lookups, "v2")).processed == 3  # corrections feed learning


def test_no_fingerprint_means_always_rebuild(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=None, lookups=lookups)
    assert process_filings(loaded_db, services).processed == 3
    assert process_filings(loaded_db, services).processed == 3
