"""One durable lifecycle for direct, batch and cached model work.

Paid jobs and provider responses survive disposable-cache deletion. Ambiguous
submissions require reconciliation; a local lease never proves remote failure.
"""

import dataclasses
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from radar.classification.base import ClassificationInput
from radar.domain.models import Classification
from radar.extraction.llm import Extracted, ExtractionInput
from radar.store.cache import KeyValueCache
from radar.store.llm_jobs import BudgetLimits, LlmJobStore, TokenPricing

PENDING = "llm-pending"  # legacy namespaces, imported without deleting their source
BATCHES = "llm-batches"
MAX_PER_BATCH = 10_000
MAX_BATCH_BYTES = 32 * 1024 * 1024


class LlmTask(Protocol):
    NAMESPACE: str
    name: str

    def request(self, item: Any) -> dict[str, Any]: ...
    def key(self, item: Any) -> str: ...
    def parse(self, message: Any, item: Any) -> Any: ...


def encode_answer(namespace: str, answer: Any) -> Any:
    if namespace == "classification":
        return {"result": answer.model_dump(mode="json") if answer else None}
    if namespace == "extraction":
        return {name: [value, quote] for name, (value, quote) in answer.items()}
    return answer


def decode_answer(namespace: str, stored: Any) -> Any:
    if namespace == "classification":
        return Classification.model_validate(stored["result"]) if stored["result"] else None
    if namespace == "extraction":
        return {name: (value, quote) for name, (value, quote) in stored.items()}
    return stored


def _decode_item(namespace: str, raw: dict[str, Any]) -> Any:
    if namespace == "classification":
        return ClassificationInput(**raw)
    if namespace == "extraction":
        return ExtractionInput(**{**raw, "fields": tuple(raw["fields"])})
    from radar.quality.model_check import CheckInput

    return CheckInput.from_json(raw)


def _json_value(value: Any) -> Any:
    """Persist SDK models and the simple fake responses used by offline tests."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, SimpleNamespace):
        return {k: _json_value(v) for k, v in vars(value).items()}
    return value


def _message(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _message(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_message(v) for v in value]
    return value


class LlmQueue:
    def __init__(
        self,
        cache: KeyValueCache,
        *,
        jobs_path: Path | str | None = None,
        limits: BudgetLimits | None = None,
        pricing: Mapping[str, TokenPricing] | None = None,
        campaign: str = "default",
    ) -> None:
        self.cache = cache
        self.jobs = LlmJobStore(
            jobs_path or cache.path.with_name("llm-jobs.sqlite"), limits=limits, pricing=pricing, campaign=campaign
        )
        self.jobs.import_legacy(str(cache.path.resolve()), cache.items(PENDING), cache.items(BATCHES))

    def add(self, task: LlmTask, item: Any, filing_key: str, *, answer: Any = None) -> None:
        self.jobs.add(
            task.key(item),
            {"task": task.name, "namespace": task.NAMESPACE, "item": dataclasses.asdict(item),
             "params": task.request(item), "filing": filing_key},
            filing_key, answer,
        )  # fmt: skip

    def queued(self, key: str) -> bool:
        job = self.jobs.get(key)
        return job is not None and job["state"] != "succeeded"

    def waiting(self, filing_key: str) -> bool:
        return self.jobs.waiting(filing_key)

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        return self.jobs.entries("pending", "budget_blocked")

    def in_flight(self) -> list[tuple[str, dict[str, Any]]]:
        return self.jobs.in_flight()

    def iter_in_flight(self) -> Iterator[tuple[str, dict[str, Any]]]:
        return self.jobs.iter_in_flight()

    def summary(self) -> dict[str, Any]:
        return self.jobs.summary()

    def active_count(self) -> int:
        self.jobs.expire_claims()
        return self.jobs.count_states("pending", "submitting", "in_flight", "received")

    def blocked_count(self) -> int:
        self.jobs.expire_claims()
        return self.jobs.count_states("blocked", "budget_blocked", "uncertain", "parse_failed", "await_parser")

    def resolve_uncertain(
        self, submission_id: str, *, provider_batch_id: str | None = None, confirm_not_accepted: bool = False
    ) -> None:
        self.jobs.resolve_uncertain(
            submission_id, provider_batch_id=provider_batch_id, confirm_not_accepted=confirm_not_accepted
        )

    def parse_saved(self, key: str, task: LlmTask) -> bool:
        job = self.jobs.get(key)
        if job is None or job["raw_message"] is None:
            return False
        try:
            item = _decode_item(job["record"]["namespace"], job["record"]["item"])
            answer = task.parse(_message(job["raw_message"]), item)
            encoded = encode_answer(task.NAMESPACE, answer)
            self.jobs.succeeded(key, encoded, parser_name=task.name)
            self.cache.put(task.NAMESPACE, key, encoded)
        except Exception as exc:
            self.jobs.parse_failed(key, f"{type(exc).__name__}: {exc}")
            return False
        return True


class Deferred:
    def __init__(self, inner: Any, cache: KeyValueCache, queue: LlmQueue | None, *, cache_only: bool = False) -> None:
        self.inner, self.cache, self.queue = inner, cache, queue
        self.cache_only = cache_only
        self.name = inner.name
        self.NAMESPACE = inner.NAMESPACE
        self.pending = False

    def key(self, item: Any) -> str:
        return str(self.inner.key(item))

    def _answer(self, item: Any, filing_key: str) -> tuple[bool, Any]:
        key = self.key(item)
        hit = self.cache.get(self.NAMESPACE, key)
        if self.queue is not None:
            if self.cache_only and hit is None and self.queue.jobs.get(key) is None:
                self.pending = False
                return False, None
            self.queue.add(self.inner, item, filing_key, answer=hit)
            job = self.queue.jobs.get(key)
            if job and job["state"] in {"received", "parse_failed", "await_parser"}:
                self.queue.parse_saved(key, self.inner)
                job = self.queue.jobs.get(key)
            if job and job["answer"] is not None:
                hit = job["answer"]
                self.cache.put(self.NAMESPACE, key, hit)
        self.pending = hit is None and self.queue is not None and not self.cache_only
        return (True, decode_answer(self.NAMESPACE, hit)) if hit is not None else (False, None)


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


class Tracked(Deferred):
    """Direct execution uses the same reservation, accounting and raw-response journal."""

    def _answer(self, item: Any, filing_key: str) -> tuple[bool, Any]:
        known, answer = super()._answer(item, filing_key)
        if known or self.queue is None or self.cache_only:
            return known, answer
        key = self.key(item)
        submission, claimed = self.queue.jobs.claim(mode="direct", limit=1, key=key)
        if not claimed:
            return False, None
        try:
            client = self.inner.client
            if hasattr(client, "with_options"):
                client = client.with_options(max_retries=0)
            response = client.beta.messages.create(
                **self.inner.request(item), betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        except Exception as exc:
            self.queue.jobs.uncertain(submission, f"{type(exc).__name__}: {exc}")
            return False, None
        self.queue.jobs.received(key, _json_value(response))
        if self.queue.parse_saved(key, self.inner):
            self.pending = False
            job = self.queue.jobs.get(key)
            return True, decode_answer(self.NAMESPACE, job["answer"] if job else None)
        return False, None


class TrackedClassifier(Tracked, DeferredClassifier):
    pass


class TrackedExtractor(Tracked, DeferredExtractor):
    pass


class TrackedVerifier(Tracked, DeferredVerifier):
    pass


class BatchClient(Protocol):
    def create(self, *, requests: list[dict[str, Any]]) -> Any: ...
    def retrieve(self, batch_id: str) -> Any: ...
    def results(self, batch_id: str) -> Iterable[Any]: ...


def submit(batches: BatchClient, queue: LlmQueue) -> list[str]:
    """Reserve before network work. A missing response is uncertain, never automatically resent."""
    ids: list[str] = []
    while True:
        submission, chunk = queue.jobs.claim(mode="batch", limit=MAX_PER_BATCH, max_bytes=MAX_BATCH_BYTES)
        if not chunk:
            if queue.jobs.has_pending():
                continue
            return ids
        requests = [{"custom_id": key, "params": entry["params"]} for key, entry in chunk]
        try:
            batch = batches.create(requests=requests)
            queue.jobs.submitted(submission, str(batch.id))
        except Exception as exc:
            queue.jobs.uncertain(submission, f"{type(exc).__name__}: {exc}")
            continue
        ids.append(str(batch.id))


def collect(batches: BatchClient, queue: LlmQueue, tasks: Iterable[LlmTask]) -> dict[str, Any]:
    by_name = {t.name: t for t in tasks}
    report: dict[str, Any] = {
        "ended": 0,
        "running": 0,
        "answers": 0,
        "failed": 0,
        "parse_failed": 0,
        "await_parser": 0,
        "collection_errors": [],
        "filings": set(),
    }
    for batch_id, batch in queue.iter_in_flight():
        try:
            if batches.retrieve(batch_id).processing_status != "ended":
                report["running"] += 1
                continue
            report["ended"] += 1
            seen: set[str] = set()
            for result in batches.results(batch_id):
                key = str(getattr(result, "custom_id", ""))
                if key not in batch["entries"]:
                    report["collection_errors"].append(f"{batch_id}: unknown result {key}")
                    continue
                seen.add(key)
                job = queue.jobs.get(key)
                if job and job["state"] == "succeeded" and job["raw_message"] is not None:
                    continue
                try:
                    if result.result.type != "succeeded":
                        queue.jobs.failed(key, str(result.result.type), _json_value(result.result))
                        report["failed"] += 1
                        continue
                    queue.jobs.received(key, _json_value(result.result.message))
                except Exception as exc:
                    # Preserve an unusable result too; repairing its parser must not repay it.
                    queue.jobs.received(key, {"unusable_provider_result": _json_value(result)})
                    queue.jobs.parse_failed(key, f"{type(exc).__name__}: {exc}")
            missing = set(batch["entries"]) - seen
            if missing:
                report["collection_errors"].append(f"{batch_id}: {len(missing)} results missing; collection retained")
            else:
                queue.jobs.finish_batch(batch_id)
        except Exception as exc:
            report["collection_errors"].append(f"{batch_id}: {type(exc).__name__}: {exc}")
    for key, entry in queue.jobs.iter_entries("received", "parse_failed", "await_parser"):
        task = by_name.get(entry["task"])
        if task is None:
            queue.jobs.parse_failed(key, f"parser unavailable: {entry['task']}", missing_parser=True)
            report["await_parser"] += 1
        elif queue.parse_saved(key, task):
            report["answers"] += 1
            report["filings"].update(queue.jobs.filings(key))
        else:
            report["parse_failed"] += 1
    report["filings"] = sorted(report["filings"])
    report["jobs"] = queue.summary()
    return report


def wait(
    batches: BatchClient,
    queue: LlmQueue,
    *,
    every_s: float = 60,
    timeout_s: float = 4 * 3600,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        running = any(batches.retrieve(i).processing_status != "ended" for i in queue.jobs.iter_in_flight_ids())
        if not running:
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(min(every_s, max(0, deadline - time.monotonic())))
