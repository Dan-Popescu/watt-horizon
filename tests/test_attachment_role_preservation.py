"""Neutral aliases of a content-deduplicated PDF cannot erase its useful form role."""

from radar.domain.enums import DocumentRole, SourceKind
from radar.pipeline.ingest import ingest_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from tests.conftest import FILLER, make_pdf


def test_neutral_alias_preserves_form_and_explicit_changes_remain_possible(db: Database, blobs: BlobStore) -> None:
    pdf = make_pdf([FILLER], {"Valeurs": "30 MW"})
    common = {"data": pdf, "source": SourceKind.DREAL_CASE_BY_CASE, "region": "hdf", "number": "012667"}
    with db.transaction():
        original = ingest_pdf(
            db, blobs, **common, url="https://example.org/cerfa.pdf", title="Formulaire stockage", filename="cerfa.pdf"
        )
        alias = ingest_pdf(
            db, blobs, **common, url="https://example.org/annexe.pdf", title="Annexe", filename="annexe.pdf"
        )
    assert alias == original
    assert db.documents(original.filing_key)[0].role is DocumentRole.FORM
    with db.transaction():
        changed = ingest_pdf(
            db,
            blobs,
            **common,
            url="https://example.org/doc.pdf",
            title="Explicit decision",
            filename="doc.pdf",
            role=DocumentRole.DECISION,
        )
    assert changed.role is DocumentRole.DECISION
