"""Model calls without a key in sight: batches (half price), cache-only checks, tiering, pre-review. A fake API."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from radar.classification.base import ClassificationInput
from radar.classification.llm import LlmClassifier, TieredClassifier
from radar.domain.enums import Category, DocumentRole, SourceKind
from radar.domain.models import Classification, Correction, DocumentRecord, Evidence, Filing, MachineOutput
from radar.extraction.llm import LlmExtractor, select_passages
from radar.pipeline import llm_batch
from radar.pipeline.filings import Services, decision_first
from radar.pipeline.llm_batch import DeferredClassifier, DeferredExtractor, DeferredVerifier, LlmQueue
from radar.pipeline.run import process_filings
from radar.quality.model_check import LlmVerifier, Reliability, build_input, reliability
from radar.reading.ocr import NullOcr
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.validation.lookups import StaticLookups


def _message(answer: dict[str, Any]) -> Any:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))])


def fake_answer(params: dict[str, Any]) -> dict[str, Any]:
    """What a model would answer, grounded in the request text (so verification keeps it)."""
    content = params["messages"][0]["content"]
    schema = params["output_config"]["format"]["schema"]
    if "category" in schema["properties"]:
        document = content.split("Document :\n", 1)[1]
        return {"category": "bess", "confidence": "high", "quote": document[:40]}
    if "verdict" in json.dumps(schema):
        return {name: {"verdict": "agree", "value": None} for name in schema["properties"]}
    return {name: {"value": None, "quote": None} for name in schema["properties"]}


class FakeBatches:
    """Mimics `client.messages.batches` (create / retrieve / results)."""

    def __init__(self) -> None:
        self.created: dict[str, list[dict[str, Any]]] = {}
        self.ended: set[str] = set()

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        batch_id = f"msgbatch_{len(self.created)}"
        self.created[batch_id] = requests
        return SimpleNamespace(id=batch_id)

    def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(processing_status="ended" if batch_id in self.ended else "in_progress")

    def results(self, batch_id: str) -> list[Any]:
        return [
            SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=_message(fake_answer(r["params"]))))
            for r in self.created[batch_id]
        ]  # fmt: skip


def _batch_services(blobs: BlobStore, lookups: StaticLookups, cache: KeyValueCache, queue: LlmQueue | None) -> Services:
    classifier, extractor, verifier = LlmClassifier("m"), LlmExtractor("m"), LlmVerifier("m-fast")
    return Services(
        blobs=blobs, ocr=NullOcr(), lookups=lookups, fingerprint="v1",
        classifier=DeferredClassifier(classifier, cache, queue),
        extractor=DeferredExtractor(extractor, cache, queue),
        verifier=DeferredVerifier(verifier, cache, queue),
        llm_queue=queue, llm_tasks=(classifier, extractor, verifier),
    )  # fmt: skip


def test_batch_round_trip(loaded_db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path) -> None:
    cache = KeyValueCache(tmp_path / "derived.sqlite")
    queue, fake = LlmQueue(cache), FakeBatches()
    services = _batch_services(blobs, lookups, cache, queue)

    first = process_filings(loaded_db, services)
    assert first.processed == 3 and len(queue.pending()) >= 2  # classification requests queued, nothing sent
    assert all("input" not in f.versions for f in loaded_db.filings() if queue.waiting(f.key))
    ids = llm_batch.submit(fake, queue)
    assert len(ids) == 1 and not queue.pending() and queue.in_flight()
    assert all(
        set(r["params"]) >= {"model", "messages", "output_config"} and "fallbacks" not in r["params"]
        for r in fake.created[ids[0]]
    )

    assert llm_batch.collect(fake, queue, services.llm_tasks)["running"] == 1  # not ended yet: nothing written
    fake.ended.add(ids[0])
    collected = llm_batch.collect(fake, queue, services.llm_tasks)
    assert collected["answers"] >= 2 and not queue.in_flight()

    services = _batch_services(blobs, lookups, cache, LlmQueue(cache))
    process_filings(loaded_db, services)  # waiting filings had no fingerprint: rebuilt with the answers
    methods = {f.number: f.classification.method for f in loaded_db.filings()}
    assert any(m.startswith("llm:m@") for m in methods.values())


def test_cache_only_mode_never_queues(
    loaded_db: Database, blobs: BlobStore, lookups: StaticLookups, tmp_path: Path
) -> None:
    cache = KeyValueCache(tmp_path / "derived.sqlite")
    services = _batch_services(blobs, lookups, cache, None)
    assert process_filings(loaded_db, services).processed == 3
    assert cache.items("llm-pending") == [] and cache.items("classification") == []


class Scripted:
    def __init__(self, name: str, answer: Classification | None, pending: bool = False) -> None:
        self.name, self.answer, self.pending, self.calls = name, answer, pending, 0

    def classify(self, item: ClassificationInput) -> Classification | None:
        self.calls += 1
        return self.answer


def test_tiered_classifier_escalates_only_doubtful_answers() -> None:
    item = ClassificationInput("k", "n", "t", "texte")
    sure = Classification(category=Category.BESS, method="fast", confidence="high", quote_verified=True)
    doubtful = sure.model_copy(update={"confidence": "medium"})
    strong_answer = sure.model_copy(update={"method": "strong", "category": Category.DATACENTER})
    for fast_answer, expected, strong_calls in ((sure, "fast", 0), (doubtful, "strong", 1), (None, "strong", 1)):
        strong = Scripted("strong", strong_answer)
        result = TieredClassifier(Scripted("fast", fast_answer), strong).classify(item)  # type: ignore[arg-type]
        assert result is not None and result.method == expected and strong.calls == strong_calls
    strong = Scripted("strong", strong_answer)
    assert TieredClassifier(Scripted("fast", None, pending=True), strong).classify(item) is None  # type: ignore[arg-type]
    assert strong.calls == 0  # batch mode: wait for the cheap answer before paying for the strong one


def _filing(confidence: float, checks: dict[str, str] | None = None) -> Filing:
    return Filing(
        key="k:1", source=SourceKind.DREAL_CASE_BY_CASE, region="hdf", number="1", title="t", document_ids=("d",),
        developer="ALPHA SAS", substation="Famars",
        evidence={"developer": Evidence(document_id="d", locator="text", quote="la société ALPHA SAS"),
                  "substation": Evidence(document_id="d", locator="text", quote="poste de Famars")},
        field_confidence={"developer": confidence, "substation": 0.99},
        classification=Classification(category=Category.BESS, method="gold"),
        machine=MachineOutput(classification=Classification(category=Category.BESS, method="gold"), values={"developer": "ALPHA SAS"}),
        model_checks=checks or {},
    )  # fmt: skip


def test_pre_review_input_and_reliability() -> None:
    text = "Vu la demande déposée par la société ALPHA SAS pour un stockage raccordé au poste de Famars."
    item = build_input(_filing(0.5), {"d": text}, {"developer": "ALPHA SAS", "substation": "Famars"})
    assert item is not None and [i.field for i in item.items] == ["developer"]  # the sure field is not re-checked
    assert "ALPHA SAS" in item.items[0].passage
    assert build_input(_filing(0.95), {"d": text}, {"developer": "ALPHA SAS"}) is None

    at = datetime(2026, 10, 13, tzinfo=UTC)
    filings = [
        _filing(0.5, {"developer": "agree"}).model_copy(
            update={"key": f"k:{i}", "strata": {"evaluation_partition": "calibration"}}
        )
        for i in range(40)
    ]
    validated = [
        Correction(
            filing_key=f.key,
            field="validated",
            value=json.dumps({"developer": "ALPHA SAS"}),
            author="Dan",
            created_at=at,
        )
        for f in filings
    ]
    rel = reliability(filings, validated)
    assert (rel.agree_seen, rel.agree_right) == (40, 40) and rel.proven
    wrong = [
        Correction(filing_key=f.key, field="developer", value="BETA", author="Dan", created_at=at) for f in filings[:10]
    ]
    assert not reliability(filings, validated + wrong).proven  # 30/40 right: not proven
    assert not Reliability(agree_seen=5, agree_right=5).proven  # too few


def test_passages_and_decision_first() -> None:
    text = "x" * 3000 + " puissance de 30 MW " + "y" * 3000 + " poste source de Famars " + "z" * 3000
    out = select_passages(text, ("power_mw",))
    assert "30 MW" in out and "Famars" not in out and len(out) < 1000
    docs = [
        DocumentRecord(
            id=i,
            source=SourceKind.DREAL_CASE_BY_CASE,
            url="u",
            title="t",
            role=role,
            filing_key="k",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            filename="f",
        )
        for i, role in (("a", DocumentRole.FORM), ("b", DocumentRole.DECISION))
    ]
    assert decision_first(docs, ["form text", "decision text"]) == ["decision text", "form text"]
