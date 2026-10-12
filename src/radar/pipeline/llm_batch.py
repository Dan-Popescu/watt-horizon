"""Message Batches for scheduled runs: half price, nobody waiting (most batches end within an hour, at most 24 h).

In batch mode a model request that misses the cache is not sent: it is queued (`LlmQueue`, in derived.sqlite)
and the filing is built without that answer. `submit` sends the queue as one batch; `collect` (next run, or
`--wait`) writes each answer into the SAME cache the direct calls use, under the same key (the request itself),
so the following build reads it like any cached answer. A filing with a request in flight never stores its input
fingerprint, so it is rebuilt once the answer is there. Review rebuilds stay on direct calls (someone is waiting).

Errored, expired or cancelled requests are simply dropped from the queue: the next build misses the cache again
and queues them again.
"""

import dataclasses
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from radar.classification.base import ClassificationInput
from radar.domain.models import Classification
from radar.extraction.llm import Extracted, ExtractionInput
from radar.store.cache import KeyValueCache

PENDING = "llm-pending"  # key -> {task, item, params, filing}
BATCHES = "llm-batches"  # batch id -> {custom_id: entry}
MAX_PER_BATCH = 10_000  # the API accepts 100k / 256 MB; smaller batches end sooner


class LlmTask(Protocol):
    NAMESPACE: str
    name: str

    def request(self, item: Any) -> dict[str, Any]: ...
    def key(self, item: Any) -> str: ...
    def parse(self, message: Any, item: Any) -> Any: ...


def encode_answer(namespace: str, answer: Any) -> Any:
    """Cache format shared with CachedClassifier / CachedExtractor / CachedVerifier."""
    if namespace == "classification":
        return {"result": answer.model_dump(mode="json") if answer else None}
    if namespace == "extraction":
        return {name: [value, quote] for name, (value, quote) in answer.items()}
    return answer  # model checks: plain JSON


def decode_answer(namespace: str, stored: Any) -> Any:
    if namespace == "classification":
        return Classification.model_validate(stored["result"]) if stored["result"] else None
    if namespace == "extraction":
        return {name: (value, quote) for name, (value, quote) in stored.items()}
    return stored


def _encode_item(item: Any) -> dict[str, Any]:
    return dataclasses.asdict(item)


def _decode_item(namespace: str, raw: dict[str, Any]) -> Any:
    if namespace == "classification":
        return ClassificationInput(**raw)
    if namespace == "extraction":
        return ExtractionInput(**{**raw, "fields": tuple(raw["fields"])})
    from radar.quality.model_check import CheckInput  # deferred: quality imports this module's codecs

    return CheckInput.from_json(raw)


class LlmQueue:
    def __init__(self, cache: KeyValueCache) -> None:
        self.cache = cache
        self._waiting: set[str] = {e["filing"] for _, e in cache.items(PENDING)}
        for _, batch in cache.items(BATCHES):
            self._waiting |= {e["filing"] for e in batch["entries"].values()}

    def add(self, task: LlmTask, item: Any, filing_key: str) -> None:
        key = task.key(item)
        self.cache.put(
            PENDING,
            key,
            {"task": task.name, "namespace": task.NAMESPACE, "item": _encode_item(item), "params": task.request(item),
             "filing": filing_key},
        )  # fmt: skip
        self._waiting.add(filing_key)

    def queued(self, key: str) -> bool:
        if self.cache.get(PENDING, key) is not None:
            return True
        return any(key in batch["entries"] for _, batch in self.cache.items(BATCHES))

    def waiting(self, filing_key: str) -> bool:
        """True while a request of this filing is queued or in flight: its build is provisional."""
        return filing_key in self._waiting

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        return self.cache.items(PENDING)

    def in_flight(self) -> list[tuple[str, dict[str, Any]]]:
        return self.cache.items(BATCHES)


class Deferred:
    """Wraps a paid task in batch mode: answer from the cache, else queue the request and answer « not yet »."""

    def __init__(self, inner: Any, cache: KeyValueCache, queue: LlmQueue | None) -> None:
        self.inner, self.cache, self.queue = inner, cache, queue
        self.name = inner.name
        self.NAMESPACE = inner.NAMESPACE
        self.pending = False  # the last call queued a request (TieredClassifier waits for it)

    def key(self, item: Any) -> str:
        return str(self.inner.key(item))

    def _answer(self, item: Any, filing_key: str) -> tuple[bool, Any]:
        key = self.inner.key(item)
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            self.pending = False
            return True, decode_answer(self.NAMESPACE, hit)
        if self.queue is None:  # cache-only mode: no call, no queue
            self.pending = False
            return False, None
        if not self.queue.queued(key):
            self.queue.add(self.inner, item, filing_key)
        self.pending = True
        return False, None


class DeferredClassifier(Deferred):
    def classify(self, item: ClassificationInput) -> Classification | None:
        known, answer = self._answer(item, item.filing_key)
        return answer if known else None


class DeferredExtractor(Deferred):
    def extract(self, item: ExtractionInput) -> Extracted:
        if not item.fields:
            return {}
        known, answer = self._answer(item, item.filing_key)
        return answer if known else {}


class DeferredVerifier(Deferred):
    def check(self, item: Any) -> dict[str, Any]:
        known, answer = self._answer(item, item.filing_key)
        return dict(answer) if known else {}


class BatchClient(Protocol):
    """The subset of `anthropic.Anthropic().messages.batches` used here (a fake in tests)."""

    def create(self, *, requests: list[dict[str, Any]]) -> Any: ...
    def retrieve(self, batch_id: str) -> Any: ...
    def results(self, batch_id: str) -> Iterable[Any]: ...


def submit(batches: BatchClient, queue: LlmQueue) -> list[str]:
    """Send every queued request (custom_id = cache key, 64 hex chars); returns the batch ids."""
    pending = queue.pending()
    ids = []
    for start in range(0, len(pending), MAX_PER_BATCH):
        chunk = pending[start : start + MAX_PER_BATCH]
        batch = batches.create(requests=[{"custom_id": key, "params": entry["params"]} for key, entry in chunk])
        queue.cache.put(BATCHES, batch.id, {"entries": dict(chunk)})
        for key, _ in chunk:
            queue.cache.delete(PENDING, key)
        ids.append(batch.id)
    return ids


def collect(batches: BatchClient, queue: LlmQueue, tasks: Iterable[LlmTask]) -> dict[str, Any]:
    """Write the answers of every ended batch into the cache. Returns counts and the filings to rebuild."""
    by_name = {t.name: t for t in tasks}
    report: dict[str, Any] = {"ended": 0, "running": 0, "answers": 0, "failed": 0, "filings": set()}
    for batch_id, batch in queue.in_flight():
        if batches.retrieve(batch_id).processing_status != "ended":
            report["running"] += 1
            continue
        report["ended"] += 1
        for result in batches.results(batch_id):
            entry = batch["entries"].get(result.custom_id)
            task = by_name.get(entry["task"]) if entry else None
            if entry is None or task is None:
                continue
            if result.result.type != "succeeded":  # errored / expired / canceled: re-queued by the next build
                report["failed"] += 1
                continue
            item = _decode_item(entry["namespace"], entry["item"])
            answer = task.parse(result.result.message, item)
            queue.cache.put(task.NAMESPACE, result.custom_id, encode_answer(task.NAMESPACE, answer))
            report["answers"] += 1
            report["filings"].add(entry["filing"])
        queue.cache.delete(BATCHES, batch_id)
    report["filings"] = sorted(report["filings"])
    return report


def wait(
    batches: BatchClient,
    queue: LlmQueue,
    *,
    every_s: float = 60,
    timeout_s: float = 4 * 3600,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until every in-flight batch has ended (True) or the timeout passes (False)."""
    deadline = time.monotonic() + timeout_s
    while True:
        running = [i for i, _ in queue.in_flight() if batches.retrieve(i).processing_status != "ended"]
        if not running:
            return True
        if time.monotonic() > deadline:
            return False
        sleep(every_s)
