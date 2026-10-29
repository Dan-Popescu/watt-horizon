"""LLM fallback extraction: only missing fields, only verified answers (quote in the text, value in the quote)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, SourceKind
from radar.extraction.llm import (
    PROMPT_VERSION,
    CachedExtractor,
    ExtractionInput,
    LlmExtractor,
    parse_value,
    verify,
)
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import process_filings
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.conftest import FILLER, make_pdf

TEXT = (
    "Le projet consiste à installer un stockage par batteries d'une puissance de 30 MW et d'une capacité de 60 MWh, "
    "raccordé au poste source de Saint-Quentin. Dossier déposé complet le 3 mars 2026."
)


class FakeClient:
    """Mimics `client.beta.messages.create(...)` and records calls."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer, self.calls = answer, 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last = kwargs
        block = SimpleNamespace(type="text", text=json.dumps(self.answer))
        return SimpleNamespace(stop_reason="end_turn", content=[block])


ANSWER = {
    "power_mw": {"value": "30", "quote": "d'une puissance de 30 MW"},
    "energy_mwh": {"value": "60 MWh", "quote": "une capacité de 60 MWh"},
    "substation": {"value": "Saint-Quentin", "quote": "raccordé au poste source de Saint-Quentin"},
    "filed_on": {"value": "2026-03-03", "quote": "déposé complet le 3 mars 2026"},
    "developer": {"value": "VOLTA SAS", "quote": "la société VOLTA SAS"},  # quote not in the text: dropped
    "siret": {"value": "82497277200027", "quote": "d'une puissance de 30 MW"},  # value not in its quote: dropped
}


def test_verification_keeps_only_grounded_values() -> None:
    item = ExtractionInput("k", "t", TEXT, tuple(ANSWER))
    kept = verify(ANSWER, item)
    assert kept == {
        "power_mw": (30.0, "d'une puissance de 30 MW"),
        "energy_mwh": (60.0, "une capacité de 60 MWh"),
        "substation": ("Saint-Quentin", "raccordé au poste source de Saint-Quentin"),
        "filed_on": ("2026-03-03", "déposé complet le 3 mars 2026"),
    }
    assert parse_value("siret", "824 972 772 00028") is None  # fails the Luhn check
    assert parse_value("power_mw", "beaucoup") is None


def test_extractor_asks_only_missing_fields_and_caches(tmp_path: Path) -> None:
    client = FakeClient(ANSWER)
    extractor = CachedExtractor(LlmExtractor("model-x", client=client), KeyValueCache(tmp_path / "c.sqlite"))  # type: ignore[arg-type]
    item = ExtractionInput("k", "t", TEXT, ("power_mw", "substation"))
    assert extractor.extract(item) == extractor.extract(item) and client.calls == 1
    schema = client.last["output_config"]["format"]["schema"]
    assert set(schema["properties"]) == {"power_mw", "substation"}


def test_pipeline_asks_llm_only_for_what_rules_missed(db: Database, blobs: BlobStore, lookups: StaticLookups) -> None:
    text = TEXT + " Porteur du projet : la société VOLTA STOCKAGE."
    answer = {**ANSWER, "developer": {"value": "VOLTA STOCKAGE", "quote": "la société VOLTA STOCKAGE"}}
    with db.transaction():
        ingest_pdf(
            db,
            blobs,
            data=make_pdf([FILLER + " " + text]),
            source=SourceKind.DREAL_CASE_BY_CASE,
            region="hauts-de-france",
            number="000002",
            url="https://example.org/d.pdf",
            title="Décision relative à un projet de stockage d’énergie par batteries",
            filename="000002-decision.pdf",
        )
    client = FakeClient(answer)
    services = Services(
        blobs=blobs,
        ocr=NullOcr(),
        classifier=GoldClassifier(
            {"000002": GoldLabel("000002", Category.BESS)}, known_keys=db.filing_keys_with_documents()
        ),
        lookups=lookups,
        extractor=LlmExtractor("model-x", client=client),  # type: ignore[arg-type]
    )
    process_filings(db, services)
    filing = db.filing("dreal_case_by_case:hauts-de-france:000002")
    assert filing is not None and client.calls == 1
    asked = set(client.last["output_config"]["format"]["schema"]["properties"])
    assert {"developer", "filed_on"} <= asked and not {"power_mw", "substation"} & asked  # rules found those
    assert filing.substation == "Saint-Quentin" and filing.evidence["substation"].locator == "text"
    assert (
        filing.developer == "VOLTA STOCKAGE"
        and filing.filed_on is not None
        and filing.filed_on.isoformat() == "2026-03-03"
    )
    assert filing.evidence["developer"].locator == f"llm-extract:model-x@{PROMPT_VERSION}"
    assert filing.versions["extraction_llm"] == f"llm-extract:model-x@{PROMPT_VERSION}"


def test_sources_have_no_control_characters() -> None:
    """Guards against regex escapes mangled into control characters (e.g. a literal backspace for \\b)."""
    root = Path(__file__).parent.parent
    bad = [
        str(p.relative_to(root))
        for p in [
            *root.joinpath("src").rglob("*.py"),
            *root.joinpath("tests").rglob("*.py"),
            *root.joinpath("src").rglob("*.html"),
        ]
        if any(ord(c) < 32 and c not in "\n\r\t" for c in p.read_text(encoding="utf-8"))
    ]
    assert bad == []
