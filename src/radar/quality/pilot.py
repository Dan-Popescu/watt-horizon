"""Private pilots compose frozen campaigns with an explicitly external discovery inventory."""

import hashlib
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from radar.domain.enums import DocumentRole, SourceKind
from radar.domain.models import Filing
from radar.evaluation import campaign, campaign_compare, campaign_review, discovery
from radar.evaluation.campaign import FrozenModel
from radar.evaluation.partitions import PartitionManifest, atomic_json, build_manifest
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore
from radar.store.db import Database


class InventoryProvenance(FrozenModel):
    """A human declaration of origin; software cannot establish its truth or independence."""

    basis: Literal["external_manual_inventory"]
    assembled_by: str = Field(min_length=1)
    assembled_on: str
    references: tuple[str, ...] = Field(min_length=1)
    inclusion_protocol: str = Field(min_length=1)
    independence_rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_origin(self) -> "InventoryProvenance":
        date.fromisoformat(self.assembled_on)
        if not all(
            value.strip() for value in (self.assembled_by, self.inclusion_protocol, self.independence_rationale)
        ):
            raise ValueError("Inventory provenance requires explicit nonblank attribution and protocol")
        for url in self.references:
            discovery.canonical(url)
        return self


class PilotSpec(FrozenModel):
    version: Literal[1] = 1
    inventory: str = Field(min_length=1)
    provenance: InventoryProvenance
    sources: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    observed_from: str | None = None
    observed_through: str | None = None
    formats: tuple[Literal["pdf", "html", "docx", "xlsx", "zip", "unknown"], ...] = ()
    roles: tuple[str, ...] = ()
    required_reads: tuple[str, ...] = ("text", "ocr", "unread")
    max_groups: int = Field(default=30, ge=1, le=1000)
    max_documents: int = Field(default=300, ge=1, le=10000)
    max_bytes: int = Field(default=268435456, ge=1)
    prospective_after: str | None = None

    @model_validator(mode="after")
    def check_dates(self) -> "PilotSpec":
        for source in self.sources:
            SourceKind(source)
        for role in self.roles:
            DocumentRole(role)
        for value in (self.observed_from, self.observed_through, self.prospective_after):
            if value:
                date.fromisoformat(value)
        if self.observed_from and self.observed_through and self.observed_from > self.observed_through:
            raise ValueError("Pilot observation interval is reversed")
        return self


class PilotManifest(FrozenModel):
    version: Literal[1] = 1
    created_at: str
    campaign_fingerprint: str
    initial_snapshot_fingerprint: str
    inventory_sha256: str
    spec: PilotSpec
    plan: dict[str, Any]
    fingerprint: str = ""

    def digest(self) -> str:
        return campaign.digest(self.model_dump(mode="json", exclude={"fingerprint"}))


class ReviewAssignment(FrozenModel):
    campaign_fingerprint: str
    filing_key: str
    slot: Literal["a", "b"]
    reviewer: str = ""


class EffortEntry(FrozenModel):
    campaign_fingerprint: str
    session_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    phase: Literal["inventory", "review", "adjudication"]
    seconds: float = Field(ge=0)
    filing_key: str | None = None


def load_spec(path: Path) -> PilotSpec:
    spec = PilotSpec.model_validate_json(path.read_text(encoding="utf-8"))
    inventory = Path(spec.inventory)
    return spec.model_copy(update={"inventory": str(inventory if inventory.is_absolute() else path.parent / inventory)})


def _inventory(path: Path) -> tuple[bytes, list[discovery.DiscoveryReference]]:
    maximum = 16 * 1024 * 1024
    if path.stat().st_size > maximum:
        raise ValueError("Pilot inventory exceeds 16 MiB")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Pilot inventory exceeds 16 MiB")
    references = [discovery.DiscoveryReference.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    identities: set[tuple[str, str, str]] = set()
    for row in references:
        identity = (row.source, row.region, discovery.canonical(row.dossier_url))
        if identity in identities:
            raise ValueError("Duplicate source/region/dossier inventory reference")
        identities.add(identity)
        urls = {discovery.canonical(url) for url in row.document_urls}
        if len(urls) != len(row.document_urls):
            raise ValueError("Duplicate expected document URL")
        if row.verified:
            if not row.reviewer.strip() or not row.project_group.strip() or not row.region.strip():
                raise ValueError("Verified inventory requires a human reviewer, group and region")
            date.fromisoformat(row.reviewed_on)
    return raw, references


def _format(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    return (
        "html"
        if extension in {"htm", "html"}
        else extension
        if extension in {"pdf", "docx", "xlsx", "zip"}
        else "unknown"
    )


def _population(
    db: Database, spec: PilotSpec, previous: PartitionManifest | None
) -> tuple[list[Filing], PartitionManifest, dict[str, dict[str, set[str]]]]:
    filings = db.filings()
    first_seen = {
        filing.key: min(document.fetched_at.astimezone(UTC).date() for document in db.documents(filing.key))
        for filing in filings
        if db.documents(filing.key)
    }
    manifest = build_manifest(
        filings,
        previous=previous,
        projects=db.projects(),
        links=campaign.current_links(db),
        first_seen=first_seen,
        test_after=date.fromisoformat(spec.prospective_after) if spec.prospective_after else None,
    )
    features = {}
    for filing in filings:
        documents = db.documents(filing.key)
        features[filing.key] = {
            "source": {filing.source.value},
            "region": {filing.region},
            "format": {document.format or _format(document.filename) for document in documents},
            "role": {document.role.value for document in documents},
            "read": {filing.strata.get("read", "unknown")},
            "year": {str(document.fetched_at.astimezone(UTC).year) for document in documents},
            "template": {filing.strata.get("template", "unknown")},
            "title": {"short" if len(filing.title.split()) < 5 else "descriptive"},
        }
    return filings, manifest, features


def _matches(db: Database, filing: Filing, spec: PilotSpec, features: dict[str, set[str]]) -> bool:
    selectors = {"source": spec.sources, "region": spec.regions, "format": spec.formats, "role": spec.roles}
    if any(values and not features[name].intersection(values) for name, values in selectors.items()):
        return False
    observed = [document.fetched_at.astimezone(UTC).date().isoformat() for document in db.documents(filing.key)]
    return any(
        (not spec.observed_from or observed_on >= spec.observed_from)
        and (not spec.observed_through or observed_on <= spec.observed_through)
        for observed_on in observed
    )


def plan(
    db: Database, spec: PilotSpec, *, previous: PartitionManifest | None = None, blobs: BlobStore | None = None
) -> dict[str, Any]:
    """Plan against all groups before filtering; a selected member always brings its whole group."""
    raw, references = _inventory(Path(spec.inventory))
    filings, partitions, features = _population(db, spec, previous)
    by_group: dict[str, list[Filing]] = defaultdict(list)
    for filing in filings:
        by_group[partitions.groups[filing.key]].append(filing)
    matching = {filing.key for filing in filings if _matches(db, filing, spec, features[filing.key])}
    eligible = {group for group, members in by_group.items() if any(filing.key in matching for filing in members)}
    chosen: list[str] = []
    represented: set[tuple[str, str]] = set()
    excluded: dict[str, str] = {}
    documents_used = bytes_used = 0
    dimensions = ("source", "region", "format", "role", "read", "year", "template", "title")

    def group_features(group: str) -> set[tuple[str, str]]:
        return {
            (name, value) for filing in by_group[group] for name in dimensions for value in features[filing.key][name]
        }

    # Greedy diversity is deterministic, with full groups counted against every budget.
    pending = set(eligible)
    while pending:
        group = min(pending, key=lambda item: (-len(group_features(item) - represented), campaign.digest(item)))
        pending.remove(group)
        ids = {document.id for filing in by_group[group] for document in db.documents(filing.key)}
        size = 0
        if blobs is not None:
            try:
                size = sum(blobs.path_for(document_id).stat().st_size for document_id in ids)
            except OSError:
                excluded[group] = "missing_original_bytes"
                continue
        reason = (
            "group_budget"
            if len(chosen) >= spec.max_groups
            else "document_budget"
            if documents_used + len(ids) > spec.max_documents
            else "byte_budget"
            if bytes_used + size > spec.max_bytes
            else None
        )
        if reason:
            excluded[group] = reason
            continue
        chosen.append(group)
        documents_used += len(ids)
        bytes_used += size
        represented.update(group_features(group))
    selected = [filing for group in chosen for filing in by_group[group]]
    required: dict[str, set[str]] = {
        "source": set(spec.sources),
        "region": set(spec.regions),
        "format": set(spec.formats),
        "role": set(spec.roles),
        "read": set(spec.required_reads),
    }
    strata = {}
    for name in dimensions:
        population = Counter(value for filing in filings for value in features[filing.key][name])
        selection = Counter(value for filing in selected for value in features[filing.key][name])
        strata[name] = {
            "population": dict(population),
            "selected": dict(selection),
            "unrepresented": sorted((set(population) | required.get(name, set())) - set(selection)),
        }
    inventory_groups = {reference.project_group for reference in references}
    return {
        "status": "planned" if selected else "blocked_empty_population",
        "network_requests": 0,
        "quality_measured": False,
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
        "inventory_references": len(references),
        "inventory_groups": len(inventory_groups),
        "verified_inventory_references": sum(reference.verified for reference in references),
        "independence": "declared_external_not_proven",
        "matching_filings": len(matching),
        "population_groups": len(by_group),
        "eligible_groups": len(eligible),
        "selected_groups": len(chosen),
        "selected_keys": sorted(filing.key for filing in selected),
        "included_outside_filter": sorted(filing.key for filing in selected if filing.key not in matching),
        "selected_documents": documents_used,
        "selected_bytes": bytes_used if blobs is not None else None,
        "bytes_checked": blobs is not None,
        "excluded_groups": dict(Counter(excluded.values())),
        "strata": strata,
        "prospective_after": partitions.test_after,
        "partitions": dict(Counter(partitions.assignments[filing.key] for filing in selected)),
        "blocked_operations": [
            "External source access requires active destination authorization; this command performs no collection",
            "Identified human readers must supply independent inventory verification and blind annotations",
        ],
    }


def prepare(
    db: Database,
    directory: Path,
    spec: PilotSpec,
    blobs: BlobStore,
    *,
    previous: PartitionManifest | None = None,
) -> dict[str, Any]:
    """Stage a complete pilot and rename once; never finalize references or expose a partial packet."""
    if directory.exists():
        raise ValueError("Pilot destination already exists; prepare a new campaign for a new population")
    report = plan(db, spec, previous=previous, blobs=blobs)
    if not report["selected_keys"]:
        raise ValueError("Pilot has no complete group within its scope and budgets; inspect pilot plan")
    checked: set[str] = set()
    for key in report["selected_keys"]:
        for document in db.documents(key):
            if document.id in checked:
                continue
            if hashlib.sha256(blobs.get(document.id, max_bytes=spec.max_bytes)).hexdigest() != document.id:
                raise ValueError("Original pilot document bytes do not match their hash")
            checked.add(document.id)
    _, partitions, _ = _population(db, spec, previous)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pilot-", dir=directory.parent) as temporary:
        staged = Path(temporary) / "prepared"
        result = campaign.prepare(
            db,
            staged,
            limit=spec.max_groups,
            double_rate=1.0,
            previous=partitions,
            include_keys=set(report["selected_keys"]),
        )
        frozen = campaign.load_campaign(staged)
        if sorted(record.filing_key for record in frozen.records) != report["selected_keys"]:
            raise ValueError("Pilot group inputs changed during preparation; inspect a fresh plan")
        initial = campaign_compare.snapshot(db, frozen, staged / "initial.json", "initial")
        raw, _ = _inventory(Path(spec.inventory))
        if hashlib.sha256(raw).hexdigest() != report["inventory_sha256"]:
            raise ValueError("Inventory changed while preparing pilot")
        inventory_path = staged / "inventory.jsonl"
        inventory_path.write_bytes(raw)
        inventory_path.chmod(0o600)
        manifest = PilotManifest(
            created_at=datetime.now(UTC).isoformat(),
            campaign_fingerprint=frozen.fingerprint,
            initial_snapshot_fingerprint=initial["fingerprint"],
            inventory_sha256=report["inventory_sha256"],
            spec=spec,
            plan=report,
        )
        manifest = manifest.model_copy(update={"fingerprint": manifest.digest()})
        atomic_json(staged / "pilot.json", manifest.model_dump(mode="json"))
        assignments = [
            ReviewAssignment(campaign_fingerprint=frozen.fingerprint, filing_key=record.filing_key, slot=slot)
            for record in frozen.records
            for slot in ("a", "b")
        ]
        assignment_path = staged / "assignments.jsonl"
        assignment_path.write_text("".join(row.model_dump_json() + "\n" for row in assignments), encoding="utf-8")
        assignment_path.chmod(0o600)
        (staged / "effort.jsonl").touch(mode=0o600)
        staged.rename(directory)
    return {**result, "directory": str(directory), "pilot_fingerprint": manifest.fingerprint, "plan": report}


def _load(directory: Path) -> tuple[PilotManifest, campaign.Campaign]:
    pilot = PilotManifest.model_validate_json((directory / "pilot.json").read_text(encoding="utf-8"))
    if pilot.fingerprint != pilot.digest():
        raise ValueError("Pilot manifest fingerprint changed")
    frozen = campaign.load_campaign(directory)
    if frozen.fingerprint != pilot.campaign_fingerprint:
        raise ValueError("Pilot campaign binding changed")
    raw, _ = _inventory(directory / "inventory.jsonl")
    if hashlib.sha256(raw).hexdigest() != pilot.inventory_sha256:
        raise ValueError("Frozen pilot inventory changed")
    initial = campaign_compare.load_snapshot(directory / "initial.json", frozen)
    if initial.fingerprint != pilot.initial_snapshot_fingerprint:
        raise ValueError("Frozen pilot initial machine snapshot changed")
    return pilot, frozen


def _effort(path: Path, frozen: campaign.Campaign) -> dict[str, Any]:
    entries = [EffortEntry.model_validate_json(line) for line in path.read_bytes().splitlines() if line.strip()]
    keys = {record.filing_key for record in frozen.records}
    identities: set[str] = set()
    for entry in entries:
        if entry.campaign_fingerprint != frozen.fingerprint or (entry.filing_key and entry.filing_key not in keys):
            raise ValueError("Effort entry does not belong to this campaign")
        if not entry.reviewer.strip() or not entry.session_id.strip() or entry.session_id in identities:
            raise ValueError("Effort requires identified reviewers and unique nonblank sessions")
        identities.add(entry.session_id)
    return {
        "status": "reported" if entries else "unmeasured",
        "sessions": len(entries),
        "seconds": sum(entry.seconds for entry in entries) if entries else None,
        "by_phase": {
            phase: sum(entry.seconds for entry in entries if entry.phase == phase)
            for phase in ("inventory", "review", "adjudication")
        },
    }


def _grouping(db: Database, inventory: list[discovery.DiscoveryReference]) -> dict[str, Any]:
    expected: dict[str, str] = {}
    for row in inventory:
        if not row.verified or not row.relevant:
            continue
        prefix = f"{discovery.SOURCES[row.source].value}:{row.region}:"
        for key in db.dossier_keys_for_root(discovery.canonical(row.dossier_url)):
            if key.startswith(prefix):
                if key in expected and expected[key] != row.project_group:
                    raise ValueError("Independent inventory assigns a filing to conflicting project groups")
                expected[key] = row.project_group
    actual = {key: project.id for project in db.projects() for key in project.filing_keys}
    comparable = set(expected) & set(actual)

    def choose_two(n: int) -> int:
        return n * (n - 1) // 2

    expected_groups = Counter(expected[key] for key in comparable)
    actual_groups = Counter(actual[key] for key in comparable)
    intersections = Counter((expected[key], actual[key]) for key in comparable)
    same_pairs = sum(choose_two(n) for n in expected_groups.values())
    different_pairs = choose_two(len(comparable)) - same_pairs
    same_in_both = sum(choose_two(n) for n in intersections.values())
    false_merges = sum(choose_two(n) for n in actual_groups.values()) - same_in_both
    false_splits = same_pairs - same_in_both
    return {
        "status": "measured" if same_pairs + different_pairs else "unmeasured",
        "mapped_filings": len(comparable),
        "expected_same_pairs": same_pairs,
        "expected_distinct_pairs": different_pairs,
        "false_merges": false_merges if different_pairs else None,
        "false_splits": false_splits if same_pairs else None,
        "scope": "verified_independent_inventory_mapped_to_current_projects",
    }


def status(
    db: Database,
    directory: Path,
    blobs: BlobStore,
    *,
    annotations: list[Path] | None = None,
    effort_path: Path | None = None,
    read: Callable[[str, bytes], PdfContent] | None = None,
) -> dict[str, Any]:
    """Expose each coverage gap separately, retaining reserved annotations only as aggregates."""
    pilot, frozen = _load(directory)
    _, inventory = _inventory(directory / "inventory.jsonl")
    assignments = [
        ReviewAssignment.model_validate_json(line)
        for line in (directory / "assignments.jsonl").read_bytes().splitlines()
        if line.strip()
    ]
    assignment_ids: set[tuple[str, str]] = set()
    keys = {record.filing_key for record in frozen.records}
    readers: dict[str, set[str]] = defaultdict(set)
    for assignment in assignments:
        identity = (assignment.filing_key, assignment.slot)
        if (
            assignment.campaign_fingerprint != frozen.fingerprint
            or assignment.filing_key not in keys
            or identity in assignment_ids
        ):
            raise ValueError("Pilot assignment binding or slot is invalid")
        assignment_ids.add(identity)
        if assignment.reviewer.strip():
            reviewer = assignment.reviewer.strip().casefold()
            if reviewer in readers[assignment.filing_key]:
                raise ValueError("Double review requires two distinct readers")
            readers[assignment.filing_key].add(reviewer)
    stale_reason = None
    try:
        campaign.check_documents(db, frozen)
    except ValueError as exc:
        stale_reason = str(exc)
    counts: Counter[str] = Counter(
        dict.fromkeys(("expected", "discovered", "downloaded", "readable", "processed", "blocked"), 0)
    )
    pieces: Counter[str] = Counter(dict.fromkeys(("expected", "discovered", "downloaded", "readable", "processed"), 0))
    blocks: Counter[str] = Counter()
    readability: dict[str, bool] = {}
    bytes_read = 0

    def readable_document(document_id: str) -> bool:
        nonlocal bytes_read
        if document_id in readability:
            return readability[document_id]
        readable = False
        try:
            size = blobs.path_for(document_id).stat().st_size
            if len(readability) >= pilot.spec.max_documents or bytes_read + size > pilot.spec.max_bytes:
                blocks["read_budget_exhausted"] += 1
            elif read is None:
                blocks["readability_not_checked"] += 1
            else:
                data = blobs.get(document_id, max_bytes=pilot.spec.max_bytes - bytes_read)
                bytes_read += len(data)
                if hashlib.sha256(data).hexdigest() != document_id:
                    raise ValueError("Original document hash changed")
                content = read(document_id, data)
                incomplete = {
                    "useful_pages_unread": bool(content.useful_unread_pages),
                    "table_reading_incomplete": bool(content.table_errors),
                    "ocr_errors": bool(content.ocr_errors),
                    "uncertain_document_segments": any(segment.uncertain for segment in content.segments),
                }
                for reason, present in incomplete.items():
                    if present:
                        blocks[reason] += 1
                for reason in content.format_errors:
                    blocks["format:" + reason] += 1
                readable = bool(content.text) and not (
                    content.unread_scanned_pages or content.format_errors or any(incomplete.values())
                )
                if not readable:
                    blocks["unread_document"] += 1
        except (OSError, ValueError) as exc:
            blocks[type(exc).__name__] += 1
        readability[document_id] = readable
        return readable

    for row in inventory:
        before_blocks = sum(blocks.values())
        counts["expected"] += 1
        root = discovery.canonical(row.dossier_url)
        prefix = f"{discovery.SOURCES[row.source].value}:{row.region}:"
        filing_keys = [key for key in db.dossier_keys_for_root(root) if key.startswith(prefix)]
        documents = [document for key in filing_keys for document in db.documents(key)]
        listed = db.listing_item_status(root)
        counts["discovered"] += bool(filing_keys or listed)
        counts["downloaded"] += bool(documents)
        counts["processed"] += bool(filing_keys) and all(
            db.processing_outcome(key) == "complete" for key in filing_keys
        )
        readable = [readable_document(document.id) for document in documents]
        counts["readable"] += bool(documents) and all(readable)
        by_url = {discovery.canonical(document.url): document for document in documents}
        edges = {discovery.canonical(edge["url"]): edge for key in filing_keys for edge in db.dossier_edges(key)}
        for piece_edge in edges.values():
            if piece_edge["state"] in {"budget", "unsupported", "invalid_document", "blocked", "failed", "fetching"}:
                blocks["piece:" + piece_edge["state"]] += 1
        for url in row.document_urls:
            canonical_url = discovery.canonical(url)
            document = by_url.get(canonical_url)
            edge = edges.get(canonical_url)
            if document is None and edge and edge["document_id"]:
                document = next((item for item in documents if item.id == edge["document_id"]), None)
            pieces["expected"] += 1
            pieces["discovered"] += bool(document or edge)
            pieces["downloaded"] += document is not None
            pieces["readable"] += document is not None and readability.get(document.id, False)
            pieces["processed"] += document is not None and db.processing_outcome(document.filing_key) == "complete"
        for key in filing_keys:
            outcome = db.processing_outcome(key)
            if outcome != "complete":
                blocks["processing:" + (outcome or "not_started")] += 1
            for scan in db.dossier_scans(key):
                if scan.get("state") not in {None, "complete"}:
                    blocks[str(scan.get("state"))] += 1
        if not documents:
            blocks["not_downloaded" if filing_keys or listed else "never_observed"] += 1
        counts["blocked"] += sum(blocks.values()) > before_blocks or not all(readable)
    paths = annotations if annotations is not None else [directory / "reviewer-a.jsonl", directory / "reviewer-b.jsonl"]
    annotation, references = campaign_review.validate(frozen, paths, blobs, read=read)
    initial = campaign_compare.load_snapshot(directory / "initial.json", frozen)
    comparison = campaign_compare.comparison(frozen, references, initial, initial)
    discovery_report = discovery.evaluate(db, directory / "inventory.jsonl")
    review_pending = (
        annotation["pending_fields"]
        or annotation["disagreements"]
        or annotation["document_relevance"].get("pending", 0)
        or annotation["document_relevance"].get("disputed", 0)
    )
    return {
        "pilot_fingerprint": pilot.fingerprint,
        "campaign_fingerprint": frozen.fingerprint,
        "status": "stale" if stale_reason else "awaiting_human_review" if review_pending else "reviewed",
        "stale_reason": stale_reason,
        "independence": "declared_external_not_proven",
        "stages": dict(counts)
        | {
            "campaign_filings": len(frozen.records),
            "assigned": sum(len(value) for value in readers.values()),
            "annotated": annotation["verified_review_rows"],
            "in_disagreement": annotation["disagreements"],
        },
        "blocked_reasons": dict(blocks),
        "blocked_events": sum(blocks.values()),
        "pieces": dict(pieces),
        "read_bytes": bytes_read,
        "annotation": annotation,
        "discovery": discovery_report,
        "values": comparison,
        "grouping": _grouping(db, inventory),
        "human_effort": _effort(effort_path or directory / "effort.jsonl", frozen),
        "representation": pilot.plan["strata"],
        "quality_measured": not stale_reason
        and bool(comparison["quality_measured"] or discovery_report["quality_measured"]),
        "network_requests": 0,
    }
