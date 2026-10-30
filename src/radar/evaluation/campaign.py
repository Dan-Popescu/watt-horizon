"""Private, blinded annotation campaigns bound to immutable source documents and grouped partitions."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.models import DocumentRecord, Filing, Link
from radar.evaluation.gold import FIELDS
from radar.evaluation.partitions import PartitionManifest, atomic_json, build_manifest
from radar.store.db import Database


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class CampaignDocument(FrozenModel):
    id: str
    url: str
    role: str
    pages: int
    source: str
    filing_key: str
    title: str
    filename: str
    audit: str | None
    fetched_at: str
    scanned_pages: tuple[int, ...]
    format: str = "pdf"
    parent_document_id: str | None = None
    member_path: str | None = None

    @classmethod
    def from_record(cls, document: DocumentRecord) -> "CampaignDocument":
        return cls(
            id=document.id,
            url=document.url,
            role=document.role.value,
            pages=document.pages,
            source=document.source.value,
            filing_key=document.filing_key,
            title=document.title,
            filename=document.filename,
            audit=document.audit,
            fetched_at=document.fetched_at.isoformat(),
            scanned_pages=document.scanned_pages,
            format=document.format,
            parent_document_id=document.parent_document_id,
            member_path=document.member_path,
        )


class CampaignRecord(FrozenModel):
    filing_key: str
    group: str
    partition: Literal["dev", "calibration", "test"]
    documents: tuple[CampaignDocument, ...]
    strata: dict[str, str]
    double_review: bool


class Campaign(FrozenModel):
    version: Literal[1] = 1
    created_at: str
    independence: Literal["unestablished"] = "unestablished"
    partition_fingerprint: str
    partition_assignments: dict[str, Literal["dev", "calibration", "test"]]
    partition_groups: dict[str, str]
    partition_test_after: str | None
    population_groups: int
    records: tuple[CampaignRecord, ...]
    fingerprint: str = ""

    def digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        # Existing PDF campaigns predate format/archive metadata. Canonicalize
        # only those new defaults so their immutable fingerprints remain valid.
        for record in payload["records"]:
            for document in record["documents"]:
                for name, default in (("format", "pdf"), ("parent_document_id", None), ("member_path", None)):
                    if document.get(name) == default:
                        document.pop(name, None)
        return digest(payload)


class Citation(FrozenModel):
    document_id: str
    page: int | None = Field(default=None, ge=1)
    locator: str = "text"
    quote: str = Field(min_length=1, max_length=600)


class Observation(FrozenModel):
    status: Literal["present", "absent", "unknown", "unreviewed"] = "unreviewed"
    value: str | float | None = None
    evidence: Citation | None = None


class Annotation(FrozenModel):
    campaign_fingerprint: str
    filing_key: str
    reviewer: str = ""
    verified: bool = False
    blind: bool = True
    reviewed_documents: tuple[str, ...] = ()
    fields: dict[str, Observation]
    document_relevance: dict[str, Observation]


ANNOTATED_FIELDS = ("category", *FIELDS)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_campaign(directory: Path) -> Campaign:
    campaign = Campaign.model_validate_json((directory / "manifest.json").read_text(encoding="utf-8"))
    if campaign.fingerprint != campaign.digest():
        raise ValueError("Campaign manifest fingerprint changed")
    frozen = frozen_partitions(campaign)
    if frozen.fingerprint != campaign.partition_fingerprint or set(frozen.groups) != set(frozen.assignments):
        raise ValueError("Campaign partition bindings changed")
    keys = [r.filing_key for r in campaign.records]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate campaign filing")
    groups: dict[str, set[str]] = defaultdict(set)
    for record in campaign.records:
        if (
            frozen.groups.get(record.filing_key) != record.group
            or frozen.assignments.get(record.filing_key) != record.partition
        ):
            raise ValueError("Campaign record does not match frozen partition bindings")
        groups[record.group].add(record.partition)
    if any(len(parts) != 1 for parts in groups.values()):
        raise ValueError("Campaign group crosses partitions")
    return campaign


def frozen_partitions(campaign: Campaign) -> PartitionManifest:
    return PartitionManifest(
        assignments=campaign.partition_assignments,
        groups=campaign.partition_groups,
        test_after=campaign.partition_test_after,
    )


def current_links(db: Database) -> Iterator[Link]:
    """Human decisions apply immediately, even before the next project rebuild."""
    from radar.linking.review import iter_apply_link_decisions

    # Stored edges can predate a withdrawal. Preserve unjournaled constraints and
    # use explicit latest withdrawals to clear only the affected stale verdicts.
    latest = {(decision.a, decision.b): decision for decision in db.iter_current_link_decisions(include_withdrawn=True)}
    yield from iter_apply_link_decisions(db.iter_links(), latest, include_missing_constraints=True)


def check_documents(db: Database, campaign: Campaign) -> dict[str, Filing]:
    records = {f.key: f for f in db.filings()}
    frozen = frozen_partitions(campaign)
    current_partitions = build_manifest(
        records.values(), previous=frozen, projects=db.projects(), links=current_links(db)
    )
    for record in campaign.records:
        expected_members = {key for key, group in frozen.groups.items() if group == record.group}
        current_group = current_partitions.groups.get(record.filing_key)
        current_members = {
            key for key, group in current_partitions.groups.items() if group == current_group and key in records
        }
        if current_members != expected_members:
            raise ValueError("Campaign group membership changed; prepare a new campaign")
    selected_documents = {d.id for record in campaign.records for d in record.documents}
    for document in db.documents():
        if document.id in selected_documents and document.filing_key not in frozen.groups:
            raise ValueError("Campaign document acquired a new group member; reprocess and prepare a new campaign")
    for record in campaign.records:
        current = db.documents(record.filing_key)
        if record.filing_key not in records or {d.id for d in current} != {d.id for d in record.documents}:
            raise ValueError("Campaign source documents changed; prepare a new campaign")
        if sorted(CampaignDocument.from_record(d).model_dump_json() for d in current) != sorted(
            d.model_dump_json() for d in record.documents
        ):
            raise ValueError("Campaign source metadata changed; prepare a new campaign")
        if set(records[record.filing_key].document_ids) != {d.id for d in record.documents}:
            raise ValueError("Filing output is stale for campaign documents")
        if db.processing_outcome(record.filing_key) != "complete":
            raise ValueError("Campaign snapshot requires complete, current processing outcomes")
    if any(db.processing_outcome(key) != "complete" for key in records):
        raise ValueError("Reprocess current population before validating campaign group independence")
    return records


def prepare(
    db: Database,
    directory: Path,
    *,
    limit: int = 100,
    double_rate: float = 0.1,
    previous: PartitionManifest | None = None,
    include_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Select complete groups with deterministic round-robin strata; export no machine values."""
    if limit <= 0 or not math.isfinite(double_rate) or not 0 <= double_rate <= 1:
        raise ValueError("Campaign limit must be positive and double rate between zero and one")
    if directory.exists():
        raise ValueError("Campaign destination already exists; immutable campaigns are never overwritten")
    filings = db.filings()
    if include_keys is not None and not include_keys <= {filing.key for filing in filings}:
        raise ValueError("Campaign selection contains an unknown filing")
    if set(db.filing_keys_with_documents()) != {f.key for f in filings}:
        raise ValueError("Process all documents before preparing an annotation campaign")
    partitions = build_manifest(filings, previous=previous, projects=db.projects(), links=current_links(db))
    by_group: dict[str, list[Filing]] = defaultdict(list)
    strata: dict[str, dict[str, str]] = {}
    for filing in filings:
        if db.processing_outcome(filing.key) != "complete":
            raise ValueError("Process all current inputs before preparing an annotation campaign")
        documents = db.documents(filing.key)
        if not documents or set(filing.document_ids) != {d.id for d in documents}:
            raise ValueError("Process all documents before preparing an annotation campaign")
        strata[filing.key] = {
            "source": filing.source.value,
            "region": filing.region,
            "first_observed_year": str(min(d.fetched_at.astimezone(UTC).year for d in documents)),
            "read": filing.strata.get("read", "unknown"),
            "template": filing.strata.get("template", "unknown"),
            "audit": "+".join(sorted({d.audit or "kept" for d in documents})),
            "roles": "+".join(sorted({d.role.value for d in documents})),
        }
        by_group[partitions.groups[filing.key]].append(filing)
    buckets: dict[str, list[str]] = defaultdict(list)
    for group, members in sorted(by_group.items()):
        if include_keys is not None and not any(member.key in include_keys for member in members):
            continue
        # Templates can be unique per file. Coarse strata prevent lexical ordering
        # of singleton buckets from silently excluding entire sources.
        bucket = json.dumps(sorted({strata[f.key]["source"] for f in members}))
        buckets[bucket].append(group)
    represented: set[tuple[str, str]] = set()
    dimensions = ("source", "region", "first_observed_year", "read", "audit", "roles", "template")

    def features(group: str) -> set[tuple[str, str]]:
        return {(name, strata[f.key][name]) for f in by_group[group] for name in dimensions}

    chosen: list[str] = []
    eligible_groups = sum(map(len, buckets.values()))
    while len(chosen) < min(limit, eligible_groups):
        for bucket in sorted(buckets):
            if buckets[bucket] and len(chosen) < limit:
                group = min(buckets[bucket], key=lambda g: (-len(features(g) - represented), digest(g)))
                chosen.append(group)
                buckets[bucket].remove(group)
                represented.update(features(group))
    double = set(sorted(chosen, key=lambda group: digest(["double", group]))[: math.ceil(len(chosen) * double_rate)])
    records = tuple(
        CampaignRecord(
            filing_key=f.key,
            group=group,
            partition=partitions.assignments[f.key],
            documents=tuple(CampaignDocument.from_record(d) for d in sorted(db.documents(f.key), key=lambda d: d.id)),
            strata=strata[f.key],
            double_review=group in double,
        )
        for group in chosen
        for f in sorted(by_group[group], key=lambda f: f.key)
    )
    campaign = Campaign(
        created_at=datetime.now(UTC).isoformat(),
        partition_fingerprint=partitions.fingerprint,
        partition_assignments=partitions.assignments,
        partition_groups=partitions.groups,
        partition_test_after=partitions.test_after,
        population_groups=len(by_group),
        records=records,
    )
    campaign = campaign.model_copy(update={"fingerprint": campaign.digest()})
    directory.mkdir(parents=True, mode=0o700)
    atomic_json(directory / "manifest.json", campaign.model_dump(mode="json"))
    for slot in ("a", "b"):
        rows = [
            Annotation(
                campaign_fingerprint=campaign.fingerprint,
                filing_key=r.filing_key,
                fields={name: Observation() for name in ANNOTATED_FIELDS},
                document_relevance={d.id: Observation() for d in r.documents},
            ).model_dump_json()
            for r in records
            if slot == "a" or r.double_review
        ]
        packet_path = directory / f"reviewer-{slot}.jsonl"
        packet_path.write_text("".join(row + "\n" for row in rows), encoding="utf-8")
        packet_path.chmod(0o600)
    return {
        "directory": str(directory),
        "fingerprint": campaign.fingerprint,
        "population_groups": len(by_group),
        "selected_groups": len(chosen),
        "filings": len(records),
        "double_review_groups": len(double),
        "partitions": dict(Counter(r.partition for r in records)),
        "independence": campaign.independence,
        "status": "awaiting_human_review" if records else "empty",
        "quality_measured": False,
        "strata": {
            name: {
                "population": dict(Counter(strata[f.key][name] for f in filings)),
                "selected": dict(Counter(r.strata[name] for r in records)),
                "unrepresented": sorted({strata[f.key][name] for f in filings} - {r.strata[name] for r in records}),
            }
            for name in dimensions
        },
    }
