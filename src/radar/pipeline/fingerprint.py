"""Fingerprints for incremental processing: a filing is rebuilt only when something it depends on changed.

A filing's input = its documents (ids, roles, titles, audit reasons) + the state shared by every build:
the code (hash of every source file of the package, so it also works without git, e.g. in Docker), the gold
files that steer builds (labels, adopted rules, aliases, templates, screen terms), the reading/classifying setup,
every active correction (they feed the learned reliabilities), and the learned state itself (arbiter, calibration,
second opinion), which converges: a run that changes nothing changes no fingerprint.
`radar process --full` ignores fingerprints.
"""

import hashlib
import json
import subprocess
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

from radar.domain.models import Correction, DocumentRecord

PACKAGE = Path(__file__).resolve().parents[1]
PROCESSING_PATHS = (
    "domain",
    "reading",
    "extraction",
    "classification",
    "validation",
    "pipeline/filings.py",
    "pipeline/corrections.py",
    "pipeline/llm_batch.py",
    "evaluation/fields.py",
    "evaluation/stats.py",
    "evaluation/partitions.py",
    "quality/arbiter.py",
    "quality/confidence.py",
    "quality/flags.py",
    "quality/locate.py",
    "quality/model_check.py",
    "quality/propagate.py",
    "quality/rulebook.py",
    "quality/templates.py",
)


def digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


@cache
def code_fingerprint() -> str:
    """Hash of every Python and data file of the package: any code change rebuilds everything."""
    h = hashlib.sha256()
    for path in sorted(PACKAGE.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json", ".csv"} and "__pycache__" not in path.parts:
            h.update(path.relative_to(PACKAGE).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:16]


def processing_fingerprint() -> str:
    """Only dependencies of filing construction invalidate its derived output.

    Reading this small set once per processing run also supports an edited checkout without stale
    process-wide memoization. Presentation, exports and collector code are not extraction inputs.
    """
    paths: list[Path] = []
    for name in PROCESSING_PATHS:
        path = PACKAGE / name
        paths.extend(path.rglob("*.py") if path.is_dir() else [path])
    return digest(
        [
            (path.relative_to(PACKAGE).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(paths)
            if path.is_file() and "__pycache__" not in path.parts
        ]
    )


@cache
def git_commit() -> str | None:
    """Short commit of the checkout (traceability only; None outside a git checkout), '+dirty' if modified."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PACKAGE, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()

    try:
        sha = git("rev-parse", "--short", "HEAD")
        return sha + ("+dirty" if git("status", "--porcelain", "--", str(PACKAGE)) else "")
    except (OSError, subprocess.SubprocessError):
        return None


def files_fingerprint(paths: Iterable[Path]) -> str:
    return digest([(p.name, hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None) for p in sorted(paths)])


def corrections_fingerprint(corrections: Iterable[Correction]) -> str:
    return digest(sorted(c.model_dump_json(exclude={"id"}) for c in corrections))


def filing_fingerprint(state: str, documents: Iterable[DocumentRecord]) -> str:
    return digest(state, sorted((d.id, d.role.value, d.title, d.audit, d.url) for d in documents))
