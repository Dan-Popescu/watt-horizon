"""Bounded offline capacity probes on disposable, explicitly synthetic workloads.

Run with ``uv run python tools/benchmark_capacity.py``. Every workload/repetition
uses a fresh interpreter; stdout contains one JSON report and never project data.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.api.queries import project_page
from radar.domain.enums import Category, Confidence, SourceKind
from radar.domain.models import Filing, Location, Power, Project
from radar.linking.linker import propose_links
from radar.linking.resolve import group_filings
from radar.store.db import Database

DEFAULT_CASES = (
    "sparse:1000",
    "sparse:5000",
    "sparse:10000",
    "dense:100",
    "dense:300",
    "dense:1000",
    "pages:1000",
    "pages:5000",
    "pages:10000",
)
MAX_RECORDS = {"sparse": 100_000, "dense": 1_500, "pages": 100_000}


def parse_case(value: str) -> tuple[str, int]:
    try:
        kind, raw_count = value.split(":")
        count = int(raw_count)
        if kind not in MAX_RECORDS or not 1 <= count <= MAX_RECORDS[kind]:
            raise ValueError
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use sparse:1..100000, dense:1..1500 or pages:1..100000") from exc
    return kind, count


def peak_rss_mib() -> float:
    # Linux ru_maxrss is KiB; macOS uses bytes. This is the whole-process HWM,
    # including native allocations and imports, not a Python allocation sample.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor


def filings(count: int, dense: bool) -> list[Filing]:
    return [
        Filing(
            key=f"manual:synthetic:{i:08}",
            source=SourceKind.MANUAL,
            region="synthetic",
            number=str(i),
            title="Synthetic capacity probe",
            document_ids=(),
            developer="SyntheticAlpha" if dense else f"Firm{i:08}",
            location=Location(commune="SyntheticTown" if dense else f"Town{i:08}"),
        )
        for i in range(count)
    ]


def matching_probe(count: int, dense: bool) -> dict[str, Any]:
    started = time.perf_counter()
    records = filings(count, dense)
    construction_s = time.perf_counter() - started
    metrics: dict[str, int] = {}
    started = time.perf_counter()
    links = propose_links(records, (), metrics=metrics)
    linking_s = time.perf_counter() - started
    expected_links = count * (count - 1) // 2 if dense else 0
    if len(links) != expected_links or metrics.get("candidate_pairs") != expected_links:
        raise RuntimeError("Full linking lost or added synthetic pairs")
    link_count = len(links)
    del links
    gc.collect()
    started = time.perf_counter()
    groups = group_filings(records)
    grouping_s = time.perf_counter() - started
    expected_groups = 1 if dense else count
    if len(groups) != expected_groups or sum(map(len, groups)) != count:
        raise RuntimeError("Grouping lost synthetic records")
    del groups
    started = time.perf_counter()
    incremental_metrics: dict[str, int] = {}
    touched = propose_links(records, (), only_keys={records[-1].key}, metrics=incremental_metrics)
    incremental_s = time.perf_counter() - started
    if len(touched) != (count - 1 if dense else 0):
        raise RuntimeError("Incremental linking lost or added synthetic pairs")
    return {
        "construction_s": construction_s,
        "linking_s": linking_s,
        "grouping_s": grouping_s,
        "incremental_one_filing_s": incremental_s,
        "candidate_pairs": metrics["candidate_pairs"],
        "links": link_count,
        "groups": expected_groups,
        "incremental_candidate_pairs": incremental_metrics["candidate_pairs"],
    }


def projects(count: int) -> Iterator[Project]:
    # Ties, unknown powers and one fixed timestamp exercise keyset boundaries.
    for i in range(count):
        power = None if i % 13 == 0 else float(1 + i % 100)
        yield Project(
            id=f"synthetic:{i:08}",
            name="Synthetic capacity probe",
            category=Category.BESS,
            developer=None,
            location=Location(commune="SyntheticTown", department="60", lat=49, lon=3),
            power=Power(mw_min=power, mw_max=power),
            substation=None,
            status="Synthetic",
            first_seen=None,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            confidence=Confidence.REVIEW,
            filing_keys=(f"manual:synthetic:{i:08}",),
        )


def percentile_nearest_rank(values: list[float], percentile: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * percentile) - 1)]


def page_probe(count: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="watt-capacity-") as directory:
        path = Path(directory) / "synthetic.sqlite"
        started = time.perf_counter()
        with Database(path) as db:
            with db.transaction():
                db.replace_projects(projects(count))
            loading_s = time.perf_counter() - started
        gc.collect()
        expected_power = sum(1 + i % 100 for i in range(count) if i % 13 != 0)
        expected_unknown = (count + 12) // 13
        latencies: list[float] = []
        seen: set[str] = set()
        max_page_items, response_bytes = 0, 0
        cursor = None
        started = time.perf_counter()
        while True:
            call_started = time.perf_counter()
            # Match the API's short-lived, read-only connection lifecycle.
            with Database(path, read_only=True) as db:
                page = project_page(db, limit=200, cursor=cursor)
                encoded = page.model_dump_json().encode("utf-8")
            latencies.append(time.perf_counter() - call_started)
            if (
                page.total != count
                or page.known_power_mw != expected_power
                or page.projects_without_power != expected_unknown
            ):
                raise RuntimeError("Paged totals do not cover the complete synthetic dataset")
            max_page_items = max(max_page_items, len(page.items))
            response_bytes += len(encoded)
            for item in page.items:
                if item.project_id in seen:
                    raise RuntimeError("Keyset traversal repeated a synthetic project")
                seen.add(item.project_id)
            cursor = page.next_cursor
            if cursor is None:
                break
            if len(latencies) > count:
                raise RuntimeError("Keyset traversal did not terminate")
        traversal_s = time.perf_counter() - started
        if len(seen) != count or max_page_items > 200:
            raise RuntimeError("Keyset traversal omitted synthetic projects or exceeded the page bound")
        return {
            "loading_s": loading_s,
            "traversal_s": traversal_s,
            "pages": len(latencies),
            "projects_seen": len(seen),
            "max_page_items": max_page_items,
            "page_latency_samples": len(latencies),
            "page_latency_p50_s": statistics.median(latencies),
            "page_latency_p95_s": percentile_nearest_rank(latencies, 0.95),
            "response_bytes": response_bytes,
            "database_bytes": path.stat().st_size,
            "known_power_mw": expected_power,
            "unknown_power_projects": expected_unknown,
        }


def worker(kind: str, count: int) -> dict[str, Any]:
    baseline = peak_rss_mib()
    started = time.perf_counter()
    measurements = page_probe(count) if kind == "pages" else matching_probe(count, kind == "dense")
    return {
        "kind": kind,
        "records": count,
        "status": "ok",
        "synthetic": True,
        "worker_elapsed_s": time.perf_counter() - started,
        "rss_after_imports_mib": baseline,
        "peak_process_rss_mib": peak_rss_mib(),
        **measurements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline synthetic capacity probes; no production data, network or model calls.",
        epilog="Example: uv run python tools/benchmark_capacity.py sparse:1000 dense:300 pages:1000 --repeats 3",
    )
    parser.add_argument("cases", nargs="*", type=parse_case, help="kind:records; bounded sparse, dense or pages")
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--timeout-seconds", type=int, default=60, choices=range(1, 61))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    cases = args.cases or [parse_case(value) for value in DEFAULT_CASES]
    if len(cases) > 12 or (args.worker and len(cases) != 1):
        parser.error("Use at most 12 scenarios; the internal worker requires exactly one")
    if args.worker:
        kind, count = cases[0]
        try:
            result = worker(kind, count)
        except Exception as exc:
            print(json.dumps({"kind": kind, "records": count, "status": "error", "error": str(exc)}))
            return 2
        print(json.dumps(result))
        return 0
    results: list[dict[str, Any]] = []
    for kind, count in cases:
        for repetition in range(1, args.repeats + 1):
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), f"{kind}:{count}", "--worker"],
                    capture_output=True,
                    text=True,
                    timeout=args.timeout_seconds,
                    check=False,
                )
                try:
                    result = json.loads(completed.stdout)
                    if not isinstance(result, dict) or completed.returncode != 0 or result.get("status") != "ok":
                        raise ValueError
                except (ValueError, TypeError):
                    result = {
                        "status": "error",
                        "returncode": completed.returncode,
                        "error": completed.stderr[-2000:] or completed.stdout[-2000:],
                    }
            except subprocess.TimeoutExpired:
                result = {"status": "timeout", "timeout_seconds": args.timeout_seconds}
            results.append(
                {
                    "kind": kind,
                    "records": count,
                    "repetition": repetition,
                    "process_wall_s": time.perf_counter() - started,
                    **result,
                }
            )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    report = {
        "schema": 1,
        "synthetic": True,
        "network_requests": 0,
        "model_calls": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_head": revision.stdout.strip() if revision.returncode == 0 else None,
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "measurements": results,
        "limitations": [
            "Synthetic workloads do not establish production capacity or extraction quality.",
            "RSS is the whole-process high-water mark, including imports, inputs and native allocations.",
            "Page timings include a read-only connection, SQL, validation and JSON encoding; exclude HTTP transport.",
            "Serial local runs do not measure concurrent users, network, PDF/OCR, model usage or cloud billing.",
        ],
    }
    print(json.dumps(report))
    return 0 if all(result["status"] == "ok" for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
