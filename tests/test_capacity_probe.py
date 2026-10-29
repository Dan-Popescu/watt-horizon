"""The offline capacity harness checks completeness rather than timing thresholds."""

import json
import subprocess
import sys
from pathlib import Path


def test_capacity_probe_checks_dense_sparse_and_complete_keyset_pages() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/benchmark_capacity.py"),
            "sparse:20",
            "dense:20",
            "pages:205",
            "storage:40",
            "--repeats",
            "1",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    report = json.loads(result.stdout)
    sparse, dense, pages, storage = report["measurements"]
    assert sparse["candidate_pairs"] == sparse["links"] == 0 and sparse["groups"] == 20
    assert dense["candidate_pairs"] == dense["links"] == 190 and dense["groups"] == 1
    assert dense["incremental_candidate_pairs"] == 19
    assert pages["projects_seen"] == 205 and pages["pages"] == 2 and pages["max_page_items"] == 200
    assert pages["unknown_power_projects"] == 16
    assert storage["links_stored"] == 40 and storage["unchanged_revision_preserved"]
    assert len(storage["stored_json_sha256"]) == 64
    assert report["network_requests"] == report["model_calls"] == 0
    assert all(measurement["peak_process_rss_mib"] > 0 for measurement in report["measurements"])


def test_capacity_probe_refuses_unbounded_dense_workload() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "tools/benchmark_capacity.py"), "dense:100000"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2
    assert "dense:1..1500" in result.stderr
