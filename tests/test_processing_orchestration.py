"""Waiting integrates the final paid stage and survives a pre-existing provider batch."""

from pathlib import Path
from typing import Any

from radar.pipeline import llm_batch
from radar.pipeline.orchestration import execute_processing
from radar.store.blobs import BlobStore
from radar.store.cache import KeyValueCache
from radar.store.db import Database
from radar.validation.lookups import StaticLookups
from tests.test_llm_batch import FakeBatches, _batch_services


class ImmediateBatches(FakeBatches):
    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        result = super().create(requests=requests)
        self.ended.add(result.id)
        return result


def test_wait_integrates_all_stages_and_finishes_without_pending_requests(
    loaded_db: Database,
    blobs: BlobStore,
    lookups: StaticLookups,
    tmp_path: Path,
) -> None:
    cache = KeyValueCache(tmp_path / "answers.sqlite")
    queue = llm_batch.LlmQueue(cache)
    services = _batch_services(blobs, lookups, cache, queue)
    provider = ImmediateBatches()
    result = execute_processing(loaded_db, services, client=provider, wait=True, timeout_s=5, poll_s=0.001)
    assert result["status"] == "complete"
    assert not queue.in_flight()
    assert not queue.pending()
    assert queue.blocked_count() == 0
    assert len(provider.created) >= 2
    assert any(f.model_checks for f in loaded_db.filings())
