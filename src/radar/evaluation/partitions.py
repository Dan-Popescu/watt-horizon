"""Frozen, versioned group assignments for development, calibration and independent evaluation."""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from radar.domain.models import Filing, Link, Project
from radar.evaluation.stats import Partition, partition_of
from radar.linking.resolve import group_filings


@dataclass
class PartitionManifest:
    version: int = 1
    assignments: dict[str, Partition] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    test_after: str | None = None

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    """Replace a complete JSON document atomically; never expose a half-written approval."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def build_manifest(
    filings: Iterable[Filing],
    *,
    previous: PartitionManifest | None = None,
    projects: Iterable[Project] = (),
    links: Iterable[Link] = (),
    first_seen: Mapping[str, date] | None = None,
    test_after: date | None = None,
) -> PartitionManifest:
    """Group shared documents and confirmed same-project records before assigning a split.

    Existing assignments are immutable. A newly discovered cross-split relationship requires a new
    evaluation campaign rather than silently contaminating a held-out population.
    """
    records = {f.key: f for f in filings}
    parents = dict.fromkeys(records, "")
    parents.update({key: key for key in records})

    def root(key: str) -> str:
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(keys: Iterable[str]) -> None:
        members = [key for key in keys if key in parents]
        if not members:
            return
        first = root(members[0])
        for key in members[1:]:
            other = root(key)
            if other != first:
                low, high = sorted((first, other))
                parents[high] = low
                first = low

    by_document: dict[str, list[str]] = {}
    for filing in records.values():
        for document_id in filing.document_ids:
            by_document.setdefault(document_id, []).append(filing.key)
    for keys in by_document.values():
        join(keys)
    for project in projects:
        join(project.filing_keys)
    links = list(links)
    for group_members in group_filings(
        records.values(),
        must=((link.a, link.b) for link in links if link.verdict == "same_project"),
        cannot=((link.a, link.b) for link in links if link.verdict in {"related", "unrelated"}),
    ):
        join(filing.key for filing in group_members)
    for link in links:
        if link.verdict == "same_project":
            join((link.a, link.b))
    # Retain previously known groups even when a file or project is temporarily absent.
    if previous:
        old_groups: dict[str, list[str]] = {}
        for key, group in previous.groups.items():
            old_groups.setdefault(group, []).append(key)
        for keys in old_groups.values():
            join(keys)
    grouped: dict[str, list[str]] = {}
    for key in records:
        grouped.setdefault(root(key), []).append(key)
    cutoff = test_after or (date.fromisoformat(previous.test_after) if previous and previous.test_after else None)
    result = PartitionManifest(
        assignments=dict(previous.assignments) if previous else {},
        groups=dict(previous.groups) if previous else {},
        test_after=cutoff.isoformat() if cutoff else None,
    )
    for keys in grouped.values():
        old = {previous.assignments[key] for key in keys if previous and key in previous.assignments}
        if len(old) > 1:
            raise ValueError("Related records cross frozen evaluation partitions; create a new campaign")
        group = min((previous.groups.get(key, key) if previous else key) for key in keys)
        if old:
            chosen = next(iter(old))
        elif cutoff and any((first_seen or {}).get(key, date.min) > cutoff for key in keys):
            chosen = "test"
        else:
            # Any known legacy test member keeps the entire connected group held out.
            parts = {partition_of(records[key]) for key in keys}
            chosen = "test" if "test" in parts else "calibration" if "calibration" in parts else "dev"
        for key in keys:
            result.assignments[key] = chosen
            result.groups[key] = group
    return result


def save_manifest(path: Path, manifest: PartitionManifest) -> None:
    atomic_json(path, asdict(manifest))


def load_manifest(path: Path) -> PartitionManifest | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1:
        raise ValueError("Unsupported evaluation partition manifest version")
    assignments = value.get("assignments", {})
    if not isinstance(assignments, dict) or any(
        part not in {"dev", "calibration", "test"} for part in assignments.values()
    ):
        raise ValueError("Invalid evaluation partition assignment")
    groups = value.get("groups", {})
    if not isinstance(groups, dict) or set(groups) != set(assignments):
        raise ValueError("Evaluation partition groups do not match assignments")
    return PartitionManifest(**value)


def with_partitions(filings: Iterable[Filing], manifest: PartitionManifest) -> list[Filing]:
    result = []
    fingerprint = manifest.fingerprint
    for filing in filings:
        if filing.key not in manifest.assignments:
            raise ValueError("Filing missing from evaluation partition manifest")
        result.append(
            filing.model_copy(
                update={
                    "strata": filing.strata
                    | {
                        "evaluation_partition": manifest.assignments[filing.key],
                        "evaluation_group": manifest.groups[filing.key],
                        "evaluation_manifest": fingerprint,
                    }
                }
            )
        )
    return result


def filings_in_partition(filings: Iterable[Filing], partition: Partition) -> list[Filing]:
    return [filing for filing in filings if partition_of(filing) == partition]


def ensure_partitions(
    path: Path,
    filings: Iterable[Filing],
    *,
    projects: Iterable[Project] = (),
    links: Iterable[Link] = (),
    first_seen: Mapping[str, date] | None = None,
    test_after: date | None = None,
) -> tuple[list[Filing], PartitionManifest]:
    records = list(filings)
    previous = load_manifest(path)
    manifest = build_manifest(
        records, previous=previous, projects=projects, links=links, first_seen=first_seen, test_after=test_after
    )
    if manifest != previous:
        save_manifest(path, manifest)
    return with_partitions(records, manifest), manifest
