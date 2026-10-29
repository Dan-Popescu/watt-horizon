"""Paid-work recovery and budgets use fake providers, never real model calls."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from radar.classification.base import ClassificationInput
from radar.classification.llm import LlmClassifier
from radar.pipeline import llm_batch
from radar.pipeline.llm_batch import DeferredClassifier, LlmQueue, TrackedClassifier
from radar.store.cache import KeyValueCache
from radar.store.llm_jobs import BudgetLimits, TokenPricing
from tests.test_llm_batch import FakeBatches, _message, fake_answer


def item(filing: str = "a", title: str = "Stockage batteries") -> ClassificationInput:
    return ClassificationInput(filing, filing, title, "Stockage de batteries de 30 MW.")


def queue_at(tmp_path: Path, **kwargs: Any) -> LlmQueue:
    return LlmQueue(KeyValueCache(tmp_path / "cache.sqlite"), jobs_path=tmp_path / "jobs.sqlite", **kwargs)


def test_shared_request_tracks_every_filing_and_survives_result_cache_deletion(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), FakeBatches()
    deferred = DeferredClassifier(task, queue.cache, queue)
    assert deferred.classify(item("a")) is None
    assert deferred.classify(item("b")) is None
    assert len(queue.pending()) == 1 and queue.waiting("a") and queue.waiting("b")
    batch_id = llm_batch.submit(fake, queue)[0]
    fake.ended.add(batch_id)
    report = llm_batch.collect(fake, queue, [task])
    assert report["filings"] == ["a", "b"]
    assert not queue.waiting("a") and not queue.waiting("b")
    queue.cache.close()
    queue.cache.path.unlink()
    restored = queue_at(tmp_path)
    answer = DeferredClassifier(task, restored.cache, restored).classify(item("c"))
    assert answer is not None and answer.category.value == "bess"
    assert restored.jobs.filings(task.key(item())) == ["a", "b", "c"]
    assert not restored.waiting("c") and llm_batch.submit(fake, restored) == []
    assert len(fake.created) == 1


def test_timeout_after_acceptance_is_not_resent_and_can_be_reconciled(tmp_path: Path) -> None:
    class AcceptedThenTimeout(FakeBatches):
        def create(self, *, requests: list[dict[str, Any]]) -> Any:
            super().create(requests=requests)
            raise TimeoutError("provider accepted, acknowledgement lost")

    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), AcceptedThenTimeout()
    queue.add(task, item(), "a")
    assert llm_batch.submit(fake, queue) == []
    restored = queue_at(tmp_path)
    assert llm_batch.submit(fake, restored) == [] and len(fake.created) == 1
    submission = restored.summary()["uncertain_submissions"][0]["id"]
    restored.resolve_uncertain(submission, provider_batch_id="msgbatch_0")
    fake.ended.add("msgbatch_0")
    assert llm_batch.collect(fake, restored, [task])["answers"] == 1
    assert not restored.waiting("a")


def test_concurrent_submitters_reserve_the_same_job_only_once(tmp_path: Path) -> None:
    queues = [queue_at(tmp_path), queue_at(tmp_path)]
    task, fake = LlmClassifier("m"), FakeBatches()
    queues[0].add(task, item(), "a")
    barrier = threading.Barrier(2)

    def send(queue: LlmQueue) -> list[str]:
        barrier.wait(timeout=5)
        return llm_batch.submit(fake, queue)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, queues))
    assert sum(map(len, results)) == 1 and len(fake.created) == 1
    assert queues[0].summary()["attempts"] == 1


def test_expired_local_lease_requires_explicit_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = queue_at(tmp_path)
    task = LlmClassifier("m")
    queue.add(task, item(), "a")
    submission, claimed = queue.jobs.claim(mode="batch", limit=1)
    assert len(claimed) == 1
    monkeypatch.setattr("radar.store.llm_jobs.time.time", lambda: 9_999_999_999.0)
    fake = FakeBatches()
    assert llm_batch.submit(fake, queue) == [] and not fake.created
    assert queue.summary()["states"] == {"uncertain": 1}
    queue.resolve_uncertain(submission, confirm_not_accepted=True)
    assert llm_batch.submit(fake, queue) == ["msgbatch_0"]


def test_model_configuration_change_preserves_raw_response_for_later_parser(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("old-model"), FakeBatches()
    queue.add(task, item(), "a")
    batch_id = llm_batch.submit(fake, queue)[0]
    fake.ended.add(batch_id)
    report = llm_batch.collect(fake, queue, [LlmClassifier("new-model")])
    assert report["await_parser"] == 1 and report["answers"] == 0
    job = queue.jobs.get(task.key(item()))
    assert job is not None and job["raw_message"]["content"][0]["text"]
    assert not queue.in_flight() and queue.waiting("a")
    assert llm_batch.collect(fake, queue_at(tmp_path), [task])["answers"] == 1
    assert len(fake.created) == 1


def test_malformed_result_does_not_block_a_later_valid_answer(tmp_path: Path) -> None:
    class MalformedFirst(FakeBatches):
        def results(self, batch_id: str) -> list[Any]:
            answers = super().results(batch_id)
            answers[0].result.message = _message({})
            return answers

    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), MalformedFirst()
    queue.add(task, item(), "a")
    queue.add(task, item("b", "Autre titre de batteries"), "b")
    fake.ended.update(llm_batch.submit(fake, queue))
    report = llm_batch.collect(fake, queue, [task])
    assert report["answers"] == 1 and report["parse_failed"] == 1
    assert queue.blocked_count() == 1 and len(fake.created) == 1


def test_parser_fix_replays_paid_response_without_repeating_provider_work(tmp_path: Path) -> None:
    class BrokenParser(LlmClassifier):
        def parse(self, message: Any, input_item: ClassificationInput) -> Any:
            raise ValueError("local parser bug")

    queue = queue_at(tmp_path)
    task, fake = BrokenParser("m"), FakeBatches()
    queue.add(task, item(), "a")
    fake.ended.update(llm_batch.submit(fake, queue))
    assert llm_batch.collect(fake, queue, [task])["parse_failed"] == 1
    assert llm_batch.collect(fake, queue, [LlmClassifier("m")])["answers"] == 1
    assert len(fake.created) == 1 and queue.summary()["attempts"] == 1


@pytest.mark.parametrize(
    "limits", [BudgetLimits(daily_usd=0), BudgetLimits(campaign_usd=0), BudgetLimits(filing_usd=0)]
)
def test_money_budgets_fail_closed_without_prices(tmp_path: Path, limits: BudgetLimits) -> None:
    queue = queue_at(tmp_path, limits=limits)
    task, fake = LlmClassifier("m"), FakeBatches()
    queue.add(task, item(), "a")
    assert llm_batch.submit(fake, queue) == [] and not fake.created
    job = queue.jobs.get(task.key(item()))
    assert job is not None and job["error"] == "missing_model_price"
    assert queue.blocked_count() == 1 and queue.active_count() == 0


@pytest.mark.parametrize("scope", ["daily_usd", "campaign_usd", "filing_usd"])
def test_budget_reservations_are_cumulative_before_responses_arrive(tmp_path: Path, scope: str) -> None:
    queue = queue_at(
        tmp_path,
        limits=BudgetLimits(**{scope: 0.005}),
        pricing={"m": TokenPricing("test-prices-v1", 1, 1)},
    )
    task, fake = LlmClassifier("m"), FakeBatches()
    queue.add(task, item(), "a")
    queue.add(task, item("a", "Autre titre"), "a")
    llm_batch.submit(fake, queue)
    assert sum(len(requests) for requests in fake.created.values()) == 1
    assert queue.summary()["states"] == {"in_flight": 1, "budget_blocked": 1}


def test_usage_and_versioned_cost_survive_cache_loss(tmp_path: Path) -> None:
    class UsageBatches(FakeBatches):
        def results(self, batch_id: str) -> list[Any]:
            results = super().results(batch_id)
            results[0].result.message.model = "m"
            results[0].result.message.usage = SimpleNamespace(input_tokens=123, output_tokens=45)
            return results

    queue = queue_at(tmp_path, pricing={"m": TokenPricing("price-2026-10-29", 1, 2, batch_multiplier=0.5)})
    task, fake = LlmClassifier("m"), UsageBatches()
    queue.add(task, item(), "a")
    fake.ended.update(llm_batch.submit(fake, queue))
    changed_prices = queue_at(tmp_path, pricing={"m": TokenPricing("new-price-version", 100, 100)})
    assert llm_batch.collect(fake, changed_prices, [task])["answers"] == 1
    summary = queue_at(tmp_path).summary()
    assert summary["input_tokens"] == 123 and summary["output_tokens"] == 45
    assert summary["accounted_usd"] == 0.000107
    row = queue.jobs.conn.execute("SELECT pricing_version,actual_model,raw_response FROM attempts").fetchone()
    assert row[0:2] == ("price-2026-10-29", "m")
    assert json.loads(row[2])["usage"]["input_tokens"] == 123


def test_cache_only_reads_durable_answers_without_scheduling_a_miss(tmp_path: Path) -> None:
    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), FakeBatches()
    queue.add(task, item(), "a")
    fake.ended.update(llm_batch.submit(fake, queue))
    assert llm_batch.collect(fake, queue, [task])["answers"] == 1
    queue.cache.delete("classification", task.key(item()))
    cached = DeferredClassifier(task, queue.cache, queue, cache_only=True)
    assert cached.classify(item("b")) is not None
    assert cached.classify(item("c", "Titre inconnu")) is None
    assert not cached.pending and not queue.pending() and queue.summary()["attempts"] == 1


def test_batch_payloads_are_split_before_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), FakeBatches()
    queue.add(task, item(), "a")
    queue.add(task, item("b", "Autre titre"), "b")
    monkeypatch.setattr(llm_batch, "MAX_BATCH_BYTES", 1800)
    assert len(llm_batch.submit(fake, queue)) == 2
    assert all(len(requests) == 1 for requests in fake.created.values())


def test_oversized_request_is_reported_without_provider_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = queue_at(tmp_path)
    task, fake = LlmClassifier("m"), FakeBatches()
    queue.add(task, item(), "a")
    monkeypatch.setattr(llm_batch, "MAX_BATCH_BYTES", 10)
    assert llm_batch.submit(fake, queue) == [] and not fake.created
    assert queue.summary()["issues"][0]["error"] == "request_payload_too_large"
    assert queue.summary()["attempts"] == 0


def test_provider_failures_stop_after_the_durable_attempt_limit(tmp_path: Path) -> None:
    class FailedBatches(FakeBatches):
        def results(self, batch_id: str) -> list[Any]:
            return [
                SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="errored"))
                for r in self.created[batch_id]
            ]

    queue = queue_at(tmp_path, limits=BudgetLimits(max_attempts=2))
    task, fake = LlmClassifier("m"), FailedBatches()
    queue.add(task, item(), "a")
    for _ in range(2):
        fake.ended.update(llm_batch.submit(fake, queue))
        assert llm_batch.collect(fake, queue, [task])["failed"] == 1
    assert llm_batch.submit(fake, queue) == [] and len(fake.created) == 2
    assert queue.summary()["states"] == {"blocked": 1}


def test_direct_call_joins_existing_batch_instead_of_paying_twice(tmp_path: Path) -> None:
    class DirectMessages:
        calls = 0

        def create(self, **params: Any) -> Any:
            self.calls += 1
            return _message(fake_answer(params))

    queue = queue_at(tmp_path)
    direct = DirectMessages()
    task = LlmClassifier("m", client=SimpleNamespace(beta=SimpleNamespace(messages=direct)))
    fake = FakeBatches()
    queue.add(task, item(), "a")
    ids = llm_batch.submit(fake, queue)
    live = TrackedClassifier(task, queue.cache, queue)
    assert live.classify(item("b")) is None and live.pending and direct.calls == 0
    fake.ended.update(ids)
    assert llm_batch.collect(fake, queue, [task])["filings"] == ["a", "b"]
    assert live.classify(item("b")) is not None and direct.calls == 0
    assert live.classify(item("c", "Nouveau dossier")) is not None and direct.calls == 1
    assert queue.summary()["attempts"] == 2


def test_legacy_batch_import_keeps_source_and_does_not_resurrect_a_finished_batch(tmp_path: Path) -> None:
    cache = KeyValueCache(tmp_path / "cache.sqlite")
    task, fake = LlmClassifier("m"), FakeBatches()
    source_item = item()
    entry = {
        "task": task.name,
        "namespace": task.NAMESPACE,
        "item": source_item.__dict__,
        "params": task.request(source_item),
        "filing": "a",
    }
    key = task.key(source_item)
    provider_id = fake.create(requests=[{"custom_id": key, "params": entry["params"]}]).id
    cache.put(llm_batch.BATCHES, provider_id, {"entries": {key: entry}})
    queue = LlmQueue(cache, jobs_path=tmp_path / "jobs.sqlite")
    fake.ended.add(provider_id)
    assert llm_batch.collect(fake, queue, [task])["answers"] == 1
    assert cache.items(llm_batch.BATCHES)  # migration never destroys its source
    reopened = queue_at(tmp_path)
    assert not reopened.in_flight() and not reopened.waiting("a")
    assert llm_batch.submit(fake, reopened) == [] and len(fake.created) == 1
