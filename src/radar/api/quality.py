"""Quality overview for the internal dashboard: last evaluation, metric trend, review queue, error clusters,
regression cases. Read-only; computed from what `radar eval`, the review loop and gold/cases already store."""

from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from radar.quality import cases
from radar.quality.errors import analyse
from radar.quality.flags import FLAGS
from radar.quality.triage import triage
from radar.store.db import Database

TREND_METRICS = (
    "screening.recall",
    "integrity.quote_not_found",
    "privacy.leaks",
    "undecided.share",
    "cases.regressed",
    "ocr.mean_word_miss_rate",
)
TREND_RUNS = 10


def quality_overview(db: Database, cases_dir: Path, today: date | None = None) -> dict[str, Any]:
    filings, corrections = db.filings(), db.corrections()
    evals = db.runs("eval", limit=TREND_RUNS)
    last = evals[0]["stats"].get("report") if evals else None
    queue = triage(filings, corrections, today or date.today())
    reasons = Counter(code for item in queue for code in item.flag_codes)
    errors = analyse(filings, corrections)
    case_status = Counter(status for case in cases.load_all(cases_dir) for status in case.status.values())
    trend = [
        {
            "at": run["finished_at"].isoformat(timespec="minutes") if run["finished_at"] else None,
            **{m: run["stats"].get("metrics", {}).get(m) for m in TREND_METRICS},
        }
        for run in reversed(evals)
    ]
    return {
        "last_eval": last,
        "trend": trend,
        "queue": {
            "size": len(queue),
            "by_flag": [
                {"code": code, "label": FLAGS[code].label_fr, "severity": FLAGS[code].severity.value, "count": n}
                for code, n in reasons.most_common()
            ],
            "top": [asdict(item) for item in queue[:10]],
        },
        "errors": {
            "reviewed": errors.reviewed,
            "with_errors": errors.with_errors,
            "test_errors": errors.test_errors,
            "clusters": errors.clusters[:10],
            "unflagged": errors.unflagged,
            "fixed": errors.fixed,
            "flag_precision": errors.flag_precision,
            "flag_recall": errors.flag_recall,
        },
        "cases": {"open": case_status.get("open", 0), "fixed": case_status.get("fixed", 0)},
    }
