"""One correction, many fixes: same-error suggestions and learned name spellings applied at every build."""

from datetime import UTC, datetime
from pathlib import Path

from starlette.testclient import TestClient

from radar.api.review import submit_review
from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Correction, Filing
from radar.pipeline.corrections import with_machine
from radar.pipeline.filings import Services
from radar.pipeline.run import link_and_build, process_filings
from radar.quality.propagate import Aliases, learn_aliases, merge, suggestions
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from radar.web.app import build_app

AT = datetime(2026, 10, 12, tzinfo=UTC)
AUBENTON = "dreal_case_by_case:hauts-de-france:010504"


def _filing(number: str, developer: str) -> Filing:
    return with_machine(
        Filing(
            key=f"d:h:{number}", source=SourceKind.DREAL_CASE_BY_CASE, region="h", number=number, title="t",
            document_ids=(), developer=developer, classification=Classification(category=Category.BESS, method="gold"),
        )
    )  # fmt: skip


def test_same_wrong_value_is_proposed_elsewhere() -> None:
    filings = [_filing("1", "la SAS Volta"), _filing("2", "la SAS Volta"), _filing("3", "Autre")]
    fix = Correction(filing_key="d:h:1", field="developer", value="VOLTA", author="Dan", created_at=AT, cause="rule")
    [s] = suggestions(filings, [fix])
    assert (s.field, s.wrong, s.right, s.numbers, s.cause) == ("developer", "la SAS Volta", "VOLTA", ["2"], "rule")
    assert learn_aliases(filings, [fix]) == {"developer": {"la sas volta": "VOLTA"}}
    unrelated = Correction(filing_key="d:h:3", field="developer", value="Société X", author="Dan", created_at=AT)
    assert "developer" not in learn_aliases(filings, [unrelated])  # a different company is not a spelling


def test_aliases_are_applied_at_every_build(
    loaded_db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path
) -> None:
    aliases = merge(Aliases(), {"developer": {"apal mw": "APAL MW (groupe APAL)"}})
    aliases.save(tmp_path / "aliases.json")
    gold = GoldClassifier({"010504": GoldLabel("010504", Category.BESS)})
    services = Services(
        blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups, aliases=Aliases.load(tmp_path / "aliases.json")
    )
    process_filings(loaded_db, services, only={AUBENTON})
    filing = loaded_db.filing(AUBENTON)
    assert filing is not None and filing.developer == "APAL MW (groupe APAL)"


def test_web_suggestions_apply(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    gold = GoldClassifier(
        {"010504": GoldLabel("010504", Category.BESS), "2025-4009": GoldLabel("2025-4009", Category.DATACENTER)}
    )
    services = Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=lookups)

    def rebuild(db: Database, key: str) -> None:
        process_filings(db, services, only={key})
        link_and_build(db)

    client = TestClient(
        build_app(loaded_db.path, with_mcp=False, review=(rebuild, dict)), base_url="http://127.0.0.1:8765"
    )
    assert client.get("/api/review/suggestions").json() == []  # nothing corrected yet
    submit_review(loaded_db, AUBENTON, author="Dan", changes={"review_decision": "subject"}, cause="rule")
    body = {
        "field": "review_decision",
        "value": "subject",
        "keys": ["dreal_case_by_case:hauts-de-france:2025-4009"],
        "author": "Dan",
        "learned_from": "010504",
    }
    assert client.post("/api/review/suggestions/apply", json=body).json() == {"applied": 1}
    bethune = loaded_db.filing("dreal_case_by_case:hauts-de-france:2025-4009")
    assert bethune is not None and bethune.review_decision.value == "subject"
