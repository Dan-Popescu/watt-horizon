"""Isolated, complete dense linking publication; synthetic inputs, no truncation.

Examples: uv run python tools/benchmark_link_pipeline.py --records 1000
Run this identical script before/after a change and retain both JSON results.
"""

import argparse
import hashlib
import json
import resource
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from radar.domain.enums import Category, SourceKind
from radar.domain.models import Classification, Filing, Location
from radar.pipeline.linking_state import incremental_link_and_build
from radar.store.db import SCHEMA_VERSION, Database


def worker(count: int) -> dict[str, Any]:
    started = time.perf_counter()
    with (
        tempfile.TemporaryDirectory(prefix="radar-link-benchmark-") as directory,
        Database(Path(directory) / "radar.sqlite") as db,
    ):
        with db.transaction():
            for index in range(count):
                filing = Filing(
                    key=f"manual:synthetic:{index:08}",
                    source=SourceKind.MANUAL,
                    region="synthetic",
                    number=str(index),
                    title="Synthetic capacity probe",
                    document_ids=(),
                    developer="SyntheticAlpha",
                    location=Location(commune="SyntheticTown"),
                )
                db.upsert_filing(
                    filing.model_copy(update={"classification": Classification(category=Category.BESS, method="test")})
                )
        report = incremental_link_and_build(db, datetime(2026, 1, 1, tzinfo=UTC))
        digest = hashlib.sha256()
        actual = 0
        for row in db.conn.execute("SELECT data FROM links ORDER BY a,b"):
            digest.update(row[0].encode())
            actual += 1
        expected = count * (count - 1) // 2
        if actual != expected or report["links"] != expected or report["projects"] != 1:
            raise RuntimeError("Complete pipeline omitted or added links/projects")
        return {
            "measured_at": datetime.now(UTC).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "source_sha256": {
                name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                for name in (
                    "src/radar/linking/linker.py",
                    "src/radar/pipeline/linking_state.py",
                    "src/radar/pipeline/projects.py",
                    "src/radar/store/link_stream.py",
                )
            },
            "records": count,
            "links": actual,
            "projects": report["projects"],
            "sha256": digest.hexdigest(),
            "elapsed_s": time.perf_counter() - started,
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "synthetic": True,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000, choices=range(2, 1501))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scratch-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.scratch_dir is not None:
            tempfile.tempdir = args.scratch_dir
        print(json.dumps(worker(args.records)))
        return
    # The parent owns scratch, so killing an interrupted child cannot strand its
    # potentially quadratic relation database.
    with tempfile.TemporaryDirectory(prefix="radar-link-benchmark-parent-") as scratch:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--records",
                str(args.records),
                "--worker",
                "--scratch-dir",
                scratch,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
    print(completed.stdout.strip())


if __name__ == "__main__":
    main()
