"""Verified local backups and atomic publication of approved SQLite datasets.

Backups are private directories, not public exports. Model jobs and human
references are durable state; the derived cache is deliberately excluded.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from radar.store.locking import process_lock

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
MANAGED_BY = "radar-snapshot"
_EXCLUDED = frozenset({"cache", "backups", "snapshots", "publications", "published", "process-lock.sqlite"})


class SnapshotError(ValueError):
    """A snapshot cannot be safely created, verified, restored or activated."""


def _identifier() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex[:12]


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError("Snapshot metadata is unavailable or malformed") from exc
    if not isinstance(payload, dict):
        raise SnapshotError("Snapshot metadata must be an object")
    return cast(dict[str, Any], payload)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, sort_keys=True, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def sync_directory(path: Path) -> None:
    """Persist directory-entry changes where POSIX directory fsync is supported."""
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            with path.open("rb") as source:
                os.fsync(source.fileno())
    for path in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
        sync_directory(path)
    sync_directory(directory)


def snapshot_database(source: Path, target: Path) -> None:
    """Copy committed WAL state into a standalone database without sidecar files."""
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as current,
        closing(sqlite3.connect(target)) as destination,
    ):
        current.backup(destination)
        destination.execute("PRAGMA journal_mode = DELETE")
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SnapshotError("SQLite snapshot failed its integrity check")


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as source:
        return source.read(16) == b"SQLite format 3\x00"


def _copy_tree(source: Path, target: Path, *, data_root: bool = False) -> None:
    if source.is_symlink():
        raise SnapshotError("Snapshots do not follow symbolic links")
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.iterdir()):
        if data_root and (
            item.name in _EXCLUDED or item.name.startswith(".") or item.name.endswith(("-wal", "-shm", "-journal"))
        ):
            continue
        if item.is_symlink():
            raise SnapshotError("Snapshots do not follow symbolic links")
        destination = target / item.name
        if item.is_dir():
            _copy_tree(item, destination)
        elif item.is_file():
            if _is_sqlite(item):
                snapshot_database(item, destination)
            else:
                shutil.copy2(item, destination)


def _copy_legacy_work(data_dir: Path, target_data: Path) -> int:
    """Preserve pre-migration paid work, but not the disposable OCR/result cache."""
    source = data_dir / "cache" / "derived.sqlite"
    if not source.exists():
        return 0
    if source.is_symlink() or source.parent.is_symlink():
        raise SnapshotError("Snapshots do not follow symbolic links")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as current:
        current.execute("BEGIN")
        tables = {row[0] for row in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kv" not in tables:
            return 0
        rows = current.execute(
            "SELECT namespace,key,value,created_at FROM kv WHERE namespace IN (?,?) ORDER BY namespace,key",
            ("llm-pending", "llm-batches"),
        ).fetchall()
        if not rows:
            return 0
        destination = target_data / "cache" / "derived.sqlite"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as preserved:
            preserved.execute(
                "CREATE TABLE kv(namespace TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(namespace,key))"
            )
            preserved.executemany("INSERT INTO kv VALUES (?,?,?,?)", rows)
            preserved.commit()
        return len(rows)


def _files(directory: Path) -> dict[str, dict[str, Any]]:
    records = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise SnapshotError("Snapshots do not follow symbolic links")
        if path.is_file() and path != directory / MANIFEST:
            records[path.relative_to(directory).as_posix()] = {
                "sha256": _hash(path),
                "bytes": path.stat().st_size,
                "sqlite": _is_sqlite(path),
            }
    return records


def verify_snapshot(directory: Path, *, kind: str | None = None) -> dict[str, Any]:
    """Verify every recorded file, unexpected files and SQLite integrity before use."""
    if directory.is_symlink() or not directory.is_dir():
        raise SnapshotError("Snapshot must be a regular directory")
    manifest = _read_json(directory / MANIFEST)
    if manifest.get("format_version") != FORMAT_VERSION or manifest.get("managed_by") != MANAGED_BY:
        raise SnapshotError("Unsupported snapshot format")
    if kind is not None and manifest.get("kind") != kind:
        raise SnapshotError("Unexpected snapshot kind")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise SnapshotError("Snapshot has no recorded files")
    actual = _files(directory)
    if actual != expected:
        raise SnapshotError("Snapshot contents or checksums do not match its manifest")
    for relative, record in actual.items():
        if record["sqlite"]:
            with closing(
                sqlite3.connect((directory / relative).resolve().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SnapshotError("SQLite snapshot failed its integrity check")
    return manifest


def seal_snapshot(
    directory: Path, *, kind: str, version: str, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record and verify all staged files before their directory can be activated."""
    manifest: dict[str, Any] = {
        **(metadata or {}),
        "format_version": FORMAT_VERSION,
        "managed_by": MANAGED_BY,
        "kind": kind,
        "version": version,
        "created_at": datetime.now(UTC).isoformat(),
        "files": _files(directory),
    }
    _write_json(directory / MANIFEST, manifest)
    verify_snapshot(directory, kind=kind)
    _sync_tree(directory)
    return manifest


def backup_dataset(
    data_dir: Path, backup_root: Path, *, gold_dir: Path | None = None, keep: int | None = None
) -> dict[str, Any]:
    """Copy committed SQLite state and durable files into a verified private snapshot."""
    data_dir, backup_root = data_dir.resolve(), backup_root.resolve()
    if not (data_dir / "radar.sqlite").is_file():
        raise SnapshotError("The source dataset does not contain radar.sqlite")
    if backup_root.is_relative_to(data_dir):
        raise SnapshotError("Store backups outside the source data directory")
    if keep is not None and keep < 1:
        raise SnapshotError("Retention must keep at least one snapshot")
    backup_root.mkdir(parents=True, exist_ok=True)
    version = _identifier()
    staging = Path(tempfile.mkdtemp(prefix=".radar-staging-", dir=backup_root))
    final = backup_root / ("watt-snapshot-" + version)
    try:
        with process_lock(data_dir):
            _copy_tree(data_dir, staging / "data", data_root=True)
            legacy_rows = _copy_legacy_work(data_dir, staging / "data")
            if gold_dir is not None:
                if not gold_dir.is_dir():
                    raise SnapshotError("The reference directory does not exist")
                _copy_tree(gold_dir, staging / "gold")
            manifest = seal_snapshot(staging, kind="backup", version=version, metadata={"legacy_job_rows": legacy_rows})
            staging.rename(final)
            sync_directory(backup_root)
        if keep is not None:
            prune_backups(backup_root, keep=keep)
        return {**manifest, "path": str(final)}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def prune_backups(backup_root: Path, *, keep: int) -> list[str]:
    """Prune only verified directories created by this module; preserve unknown files."""
    if keep < 1:
        raise SnapshotError("Retention must keep at least one snapshot")
    managed: list[Path] = []
    for path in backup_root.glob("watt-snapshot-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            verify_snapshot(path, kind="backup")
        except (SnapshotError, sqlite3.Error):
            continue
        managed.append(path)
    removed = []
    for path in sorted(managed, reverse=True)[keep:]:
        shutil.rmtree(path)
        removed.append(path.name)
    if removed:
        sync_directory(backup_root)
    return removed


def _empty_destination(path: Path) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise SnapshotError("Restore destination must be absent or an empty directory")


def restore_dataset(snapshot_dir: Path, destination: Path, *, gold_destination: Path | None = None) -> dict[str, Any]:
    """Validate first, restore to fresh locations, and never overwrite existing work."""
    manifest = verify_snapshot(snapshot_dir, kind="backup")
    if not (snapshot_dir / "data" / "radar.sqlite").is_file():
        raise SnapshotError("Snapshot has no dataset database")
    destination = destination.absolute()
    if destination.resolve().is_relative_to(snapshot_dir.resolve()):
        raise SnapshotError("Restore outside the verified backup directory")
    _empty_destination(destination)
    has_gold = (snapshot_dir / "gold").is_dir()
    gold_target = gold_destination.absolute() if gold_destination is not None else destination / "gold"
    if has_gold and destination.is_relative_to(gold_target):
        raise SnapshotError("The reference destination cannot replace the dataset directory")
    external_gold = has_gold and not gold_target.is_relative_to(destination)
    if external_gold:
        _empty_destination(gold_target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".radar-restore-", dir=destination.parent))
    gold_staging: Path | None = None
    activated_gold = False
    try:
        shutil.copytree(snapshot_dir / "data", staging / "data")
        if has_gold:
            if external_gold:
                gold_target.parent.mkdir(parents=True, exist_ok=True)
                gold_staging = Path(tempfile.mkdtemp(prefix=".radar-restore-gold-", dir=gold_target.parent))
                shutil.copytree(snapshot_dir / "gold", gold_staging / "gold")
                _sync_tree(gold_staging / "gold")
            else:
                relative = gold_target.relative_to(destination)
                gold_stage_target = staging / "data" / relative
                if gold_stage_target.exists():
                    for source in (snapshot_dir / "gold").rglob("*"):
                        if source.is_file():
                            overlap = gold_stage_target / source.relative_to(snapshot_dir / "gold")
                            if overlap.exists() and (not overlap.is_file() or _hash(overlap) != _hash(source)):
                                raise SnapshotError("The reference destination conflicts with restored data")
                shutil.copytree(snapshot_dir / "gold", gold_stage_target, dirs_exist_ok=True)
        _sync_tree(staging / "data")
        _empty_destination(destination)
        if external_gold and gold_staging is not None:
            _empty_destination(gold_target)
            if gold_target.exists():
                gold_target.rmdir()
            (gold_staging / "gold").rename(gold_target)
            sync_directory(gold_target.parent)
            activated_gold = True
        if destination.exists():
            destination.rmdir()
        (staging / "data").rename(destination)
        sync_directory(destination.parent)
    except BaseException:
        if activated_gold and gold_staging is not None:
            gold_target.rename(gold_staging / "gold")
        raise
    finally:
        shutil.rmtree(staging)
        if gold_staging is not None:
            shutil.rmtree(gold_staging)
    return {
        "version": manifest["version"],
        "data_dir": str(destination),
        "gold_dir": str(gold_target) if has_gold else None,
    }
