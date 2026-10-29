"""Local coordination and operational observations for a single-host deployment."""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from radar.store.locking import ProcessBusyError as ProcessBusyError
from radar.store.locking import process_lock as process_lock
from radar.store.readiness import candidate_readiness
from radar.store.snapshots import SnapshotError, seal_snapshot, snapshot_database, sync_directory, verify_snapshot


def _atomic_pointer(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            json.dump(payload, destination, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _version_path(publications_dir: Path, version: str) -> Path:
    if not version or Path(version).name != version or version in {".", ".."}:
        raise SnapshotError("Invalid published dataset version")
    path = publications_dir / "versions" / version
    if path.is_symlink():
        raise SnapshotError("Published versions cannot be symbolic links")
    return path


def _approved_version(publications_dir: Path, version: str) -> tuple[Path, dict[str, Any]]:
    from radar.evaluation.baseline import approved_for_dataset, dataset_fingerprint

    path = _version_path(publications_dir, version)
    manifest = verify_snapshot(path, kind="dataset")
    fingerprint = dataset_fingerprint(path / "radar.sqlite")
    if manifest.get("dataset_fingerprint") != fingerprint:
        raise SnapshotError("Published dataset identity does not match its manifest")
    approved_for_dataset(path / "approval.json", fingerprint)
    return path, manifest


def publish_dataset(db_path: Path, publications_dir: Path, *, baseline_path: Path) -> dict[str, Any]:
    """Freeze an evaluated dataset and switch readers atomically to that version.

    Approval is checked against the copied database, never merely against a path or
    the presence of a previous successful run. A failed candidate leaves the active
    version untouched. This publishes data locally; it does not deploy a server.
    """
    from radar.evaluation.baseline import approved_for_dataset, dataset_fingerprint

    if not db_path.is_file():
        raise SnapshotError("Dataset database does not exist")
    versions = publications_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    version = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:12]
    staging = Path(tempfile.mkdtemp(prefix=".radar-publishing-", dir=versions))
    target = versions / version
    try:
        with process_lock(db_path.parent):
            readiness = candidate_readiness(db_path.parent)
            if not readiness["ready"]:
                raise SnapshotError("Candidate processing is incomplete: " + ", ".join(readiness["reasons"]))
            snapshot_database(db_path, staging / "radar.sqlite")
            fingerprint = dataset_fingerprint(staging / "radar.sqlite")
            approval = approved_for_dataset(baseline_path, fingerprint)
            with (staging / "approval.json").open("x", encoding="utf-8") as destination:
                json.dump(approval, destination, ensure_ascii=False, sort_keys=True, indent=2)
            seal_snapshot(staging, kind="dataset", version=version, metadata={"dataset_fingerprint": fingerprint})
            for name in ("radar.sqlite", "approval.json", "manifest.json"):
                (staging / name).chmod(0o444)
            staging.rename(target)
            sync_directory(versions)
            return activate_dataset(publications_dir, version)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def activate_dataset(publications_dir: Path, version: str) -> dict[str, Any]:
    """Activate or roll back to a fully verified, independently approved version."""
    path, manifest = _approved_version(publications_dir, version)
    pointer = {
        "format_version": 1,
        "version": version,
        "dataset_fingerprint": manifest["dataset_fingerprint"],
        "activated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_pointer(publications_dir / "active.json", pointer)
    return {**pointer, "db_path": str(path / "radar.sqlite")}


def _resolve_pointer(publications_dir: Path, raw: bytes) -> Path:
    try:
        pointer = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SnapshotError("Invalid active dataset pointer") from exc
    if (
        not isinstance(pointer, dict)
        or pointer.get("format_version") != 1
        or not isinstance(pointer.get("version"), str)
    ):
        raise SnapshotError("Invalid active dataset pointer")
    path, manifest = _approved_version(publications_dir, pointer["version"])
    if pointer.get("dataset_fingerprint") != manifest["dataset_fingerprint"]:
        raise SnapshotError("Active pointer and dataset identity differ")
    return path / "radar.sqlite"


def active_dataset_path(publications_dir: Path) -> Path:
    """Resolve a verified immutable version; one request can retain this path."""
    try:
        raw = (publications_dir / "active.json").read_bytes()
    except OSError as exc:
        raise SnapshotError("No valid active dataset is published") from exc
    return _resolve_pointer(publications_dir, raw)


class PublishedDataset:
    """Resolve atomic activations per request, verifying each pointer version once.

    A request keeps one immutable database path. Its identity, contents and approval
    are validated together when the exact pointer bytes change. Reading the bytes
    before caching avoids pairing a new pointer's identity with an old path. The
    versions directory must remain writable only by the publishing operator.
    """

    def __init__(self, publications_dir: Path) -> None:
        self.publications_dir = publications_dir
        self._pointer: bytes | None = None
        self._path: Path | None = None
        self._lock = threading.Lock()

    def __call__(self) -> Path:
        try:
            pointer = (self.publications_dir / "active.json").read_bytes()
        except OSError as exc:
            raise SnapshotError("No valid active dataset is published") from exc
        with self._lock:
            if pointer != self._pointer or self._path is None:
                path = _resolve_pointer(self.publications_dir, pointer)
                self._path = path
                self._pointer = pointer
            return self._path


def dataset_status(publications_dir: Path) -> dict[str, Any]:
    """Report publication separately from working-database health."""
    try:
        path = active_dataset_path(publications_dir)
    except (ValueError, OSError, sqlite3.Error):
        return {"published": False, "reason": "no_verified_active_dataset"}
    manifest = verify_snapshot(path.parent, kind="dataset")
    return {
        "published": True,
        "version": manifest["version"],
        "dataset_fingerprint": manifest["dataset_fingerprint"],
        "created_at": manifest["created_at"],
        "db_path": str(path),
    }


def operational_status(data_dir: Path, *, now: datetime | None = None, max_age_s: float = 3 * 86400) -> dict[str, Any]:
    """Observe availability and freshness separately, without creating an absent DB."""
    if max_age_s < 0:
        raise ValueError("max_age_s must be nonnegative")
    from radar.store.db import SCHEMA_VERSION

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    path = data_dir / "radar.sqlite"
    result: dict[str, Any] = {
        "database": {"ready": False, "reason": "missing"},
        "freshness": {"status": "unknown", "last_completed_link": None},
    }
    if data_dir.exists():
        disk = shutil.disk_usage(data_dir)
        result["disk"] = {"free_bytes": disk.free, "total_bytes": disk.total}
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if schema != SCHEMA_VERSION or not {"projects", "runs"} <= names:
                result["database"] = {"ready": False, "reason": "schema", "schema_version": schema}
                return result
            result["database"] = {"ready": True, "schema_version": schema}
            row = connection.execute(
                "SELECT finished_at FROM runs WHERE kind='link' AND finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                at = datetime.fromisoformat(row[0])
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                age = max(0.0, (now - at).total_seconds())
                result["freshness"] = {
                    "status": "fresh" if age <= max_age_s else "stale",
                    "last_completed_link": at.isoformat(),
                    "age_seconds": age,
                }
    except (sqlite3.Error, ValueError, OSError):
        result["database"] = {"ready": False, "reason": "unreadable"}
    return result
