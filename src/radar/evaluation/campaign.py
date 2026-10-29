"""Private, blinded annotation campaigns bound to immutable source documents and grouped partitions."""

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.models import Filing
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
    population_groups: int
    records: tuple[CampaignRecord, ...]
    fingerprint: str = ""

    def digest(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"fingerprint"}))


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
    keys = [r.filing_key for r in campaign.records]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate campaign filing")
    groups: dict[str, set[str]] = defaultdict(set)
    for record in campaign.records:
        groups[record.group].add(record.partition)
    if any(len(parts) != 1 for parts in groups.values()):
        raise ValueError("Campaign group crosses partitions")
    return campaign


def check_documents(db: Database, campaign: Campaign) -> dict[str, Filing]:
    records = {f.key: f for f in db.filings()}
    for record in campaign.records:
        current = db.documents(record.filing_key)
        if record.filing_key not in records or {d.id for d in current} != {d.id for d in record.documents}:
            raise ValueError("Campaign source documents changed; prepare a new campaign")
        if {(d.id, d.url, d.role.value, d.pages) for d in current} != {
            (d.id, d.url, d.role, d.pages) for d in record.documents
        }:
            raise ValueError("Campaign source metadata changed; prepare a new campaign")
        if set(records[record.filing_key].document_ids) != {d.id for d in record.documents}:
            raise ValueError("Filing output is stale for campaign documents")
    return records


def prepare(
    db: Database,
    directory: Path,
    *,
    limit: int = 100,
    double_rate: float = 0.1,
    previous: PartitionManifest | None = None,
) -> dict[str, Any]:
    """Select complete groups with deterministic round-robin strata; export no machine values."""
    if limit <= 0 or not math.isfinite(double_rate) or not 0 <= double_rate <= 1:
        raise ValueError("Campaign limit must be positive and double rate between zero and one")
    if directory.exists():
        raise ValueError("Campaign destination already exists; immutable campaigns are never overwritten")
    filings = db.filings()
    partitions = build_manifest(filings, previous=previous, projects=db.projects(), links=db.links())
    by_group: dict[str, list[Filing]] = defaultdict(list)
    strata: dict[str, dict[str, str]] = {}
    for filing in filings:
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
        # Templates can be unique per file. Coarse strata prevent lexical ordering
        # of singleton buckets from silently excluding entire sources.
        bucket = json.dumps(sorted({strata[f.key]["source"] for f in members}))
        buckets[bucket].append(group)
    represented: set[tuple[str, str]] = set()
    dimensions = ("source", "region", "first_observed_year", "read", "audit", "roles", "template")

    def features(group: str) -> set[tuple[str, str]]:
        return {(name, strata[f.key][name]) for f in by_group[group] for name in dimensions}

    chosen: list[str] = []
    while len(chosen) < min(limit, len(by_group)):
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
            documents=tuple(
                CampaignDocument(id=d.id, url=d.url, role=d.role.value, pages=d.pages)
                for d in sorted(db.documents(f.key), key=lambda d: d.id)
            ),
            strata=strata[f.key],
            double_review=group in double,
        )
        for group in chosen
        for f in sorted(by_group[group], key=lambda f: f.key)
    )
    campaign = Campaign(
        created_at=datetime.now(UTC).isoformat(),
        partition_fingerprint=partitions.fingerprint,
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
