"""Validate explicit human observations; unknown or unfinished reviews never become absent gold."""

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from radar.domain.enums import Category, ReviewDecision
from radar.domain.fields import Value
from radar.domain.ids import luhn_ok
from radar.domain.text import contains_quote, fold
from radar.evaluation.campaign import ANNOTATED_FIELDS, Annotation, Campaign, CampaignRecord, Observation
from radar.extraction.llm import value_in_quote
from radar.reading.pdf import PdfContent, PdfReader
from radar.store.blobs import BlobStore


def exact_same(expected: Value, actual: Value) -> bool:
    """Normalize text, but never hide a numeric difference behind the business tolerance."""
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return float(expected) == float(actual)
    return fold(str(expected)) == fold(str(actual))


def _value(name: str, value: Value) -> None:
    if value is None or value == "" or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("Present annotation needs a finite, explicit value")
    if name in {"power_mw", "power_mw_min", "energy_mwh"}:
        if not isinstance(value, int | float) or value < 0:
            raise ValueError("Quantity annotations must be non-negative numbers in MW or MWh")
    elif not isinstance(value, str):
        raise ValueError("Text annotation must be a string")
    if name == "category":
        assert isinstance(value, str)
        Category(value)
    if name == "review_decision":
        assert isinstance(value, str)
        ReviewDecision(value)
    if name == "filed_on":
        assert isinstance(value, str)
        date.fromisoformat(value)
    if name == "siret":
        assert isinstance(value, str)
        if len(value) != 14 or not value.isascii() or not value.isdecimal() or not luhn_ok(value):
            raise ValueError("SIRET annotation must contain 14 valid digits")
    if name == "document_relevance" and value not in {"relevant", "irrelevant"}:
        raise ValueError("Document relevance must be relevant or irrelevant")


def _observation(
    name: str,
    observation: Observation,
    row: Annotation,
    record: CampaignRecord,
    content: Callable[[str], PdfContent],
) -> None:
    ids = {d.id for d in record.documents}
    if observation.status != "present":
        if observation.value is not None or observation.evidence is not None:
            raise ValueError("Only present observations may contain a value or citation")
        if observation.status == "absent" and set(row.reviewed_documents) != ids:
            raise ValueError("Absent requires an explicit review of every campaign document")
        if name == "document_relevance" and observation.status == "absent":
            raise ValueError("Document relevance uses relevant/irrelevant, never absent")
        return
    _value(name, observation.value)
    assert observation.value is not None
    evidence = observation.evidence
    if evidence is None or evidence.document_id not in ids:
        raise ValueError("Present observation needs a citation in a campaign document")
    if evidence.document_id not in row.reviewed_documents:
        raise ValueError("Cited document must be explicitly reviewed")
    pdf = content(evidence.document_id)
    if evidence.locator.startswith("field:"):
        if evidence.page is not None:
            raise ValueError("A form-field citation uses its locator rather than an invented page")
        text = pdf.fields.get(evidence.locator.removeprefix("field:"), "")
    else:
        if evidence.page is None or evidence.page > pdf.pages:
            raise ValueError("Text citation needs a valid PDF page")
        text = pdf.ocr_texts.get(evidence.page) or pdf.page_texts[evidence.page - 1]
    if not contains_quote(text, evidence.quote):
        raise ValueError("Annotation citation is absent from the specified source page or field")
    if name not in {"category", "review_decision", "document_relevance"} and not value_in_quote(
        name, observation.value, evidence.quote
    ):
        raise ValueError("Annotation value is unsupported by its citation")


def validate(
    campaign: Campaign,
    paths: list[Path],
    blobs: BlobStore,
    *,
    read: Callable[[str, bytes], PdfContent] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Value]]]:
    """Return report and consensus references; no global gold files or corrections are modified."""
    records = {r.filing_key: r for r in campaign.records}
    reviews: dict[str, list[Annotation]] = defaultdict(list)
    reader = PdfReader()
    cache: dict[str, PdfContent] = {}

    def content(document_id: str) -> PdfContent:
        if document_id not in cache:
            data = blobs.get(document_id, max_bytes=reader.max_bytes)
            if hashlib.sha256(data).hexdigest() != document_id:
                raise ValueError("Campaign document bytes do not match their hash")
            cache[document_id] = (read or reader.read)(document_id, data)
        return cache[document_id]

    states: Counter[str] = Counter()
    artifact_hashes = []
    verified_rows = 0
    for path in paths:
        encoded = path.read_bytes()
        artifact_hashes.append(hashlib.sha256(encoded).hexdigest())
        for line in encoded.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = Annotation.model_validate_json(line)
            if row.campaign_fingerprint != campaign.fingerprint or row.filing_key not in records:
                raise ValueError("Annotation does not belong to this frozen campaign")
            record = records[row.filing_key]
            ids = {d.id for d in record.documents}
            if set(row.fields) != set(ANNOTATED_FIELDS) or set(row.document_relevance) != ids:
                raise ValueError("Annotation fields or document inventory changed")
            if not set(row.reviewed_documents) <= ids or len(set(row.reviewed_documents)) != len(
                row.reviewed_documents
            ):
                raise ValueError("Invalid reviewed document inventory")
            if not row.verified:
                continue
            if not row.reviewer.strip() or not row.blind:
                raise ValueError("Verified campaign annotations require an identified blind reviewer")
            if any(
                old.reviewer.casefold().strip() == row.reviewer.casefold().strip() for old in reviews[row.filing_key]
            ):
                raise ValueError("Duplicate reviewer for a campaign filing")
            for name, observation in row.fields.items():
                _observation(name, observation, row, record, content)
                states[observation.status] += 1
            for document_id, observation in row.document_relevance.items():
                _observation("document_relevance", observation, row, record, content)
                if (
                    observation.status == "present"
                    and observation.evidence
                    and observation.evidence.document_id != document_id
                ):
                    raise ValueError("Document relevance must cite that document")
            reviews[row.filing_key].append(row)
            verified_rows += 1
    references: dict[str, dict[str, Value]] = {}
    disagreements = pending = accepted = 0
    relevance: Counter[str] = Counter()
    for key, record in records.items():
        for name in ANNOTATED_FIELDS:
            observations = [r.fields[name] for r in reviews[key]]
            usable = [o for o in observations if o.status in {"present", "absent"}]
            required = 2 if record.double_review else 1
            if len(usable) < required:
                pending += 1
                continue
            first = usable[0]
            if any(o.status != first.status or not exact_same(o.value, first.value) for o in usable[1:]):
                disagreements += 1
                continue
            references.setdefault(key, {})[name] = first.value
            accepted += 1
        for document in record.documents:
            observations = [r.document_relevance[document.id] for r in reviews[key]]
            usable = [o for o in observations if o.status == "present"]
            required = 2 if record.double_review else 1
            if len(usable) < required:
                relevance["pending"] += 1
            elif any(o.value != usable[0].value for o in usable[1:]):
                relevance["disputed"] += 1
            else:
                relevance[str(usable[0].value)] += 1
    report = {
        "campaign_fingerprint": campaign.fingerprint,
        "annotation_fingerprint": hashlib.sha256("".join(sorted(artifact_hashes)).encode()).hexdigest(),
        "independence": campaign.independence,
        "verified_review_rows": verified_rows,
        "accepted_fields": accepted,
        "pending_fields": pending,
        "disagreements": disagreements,
        "observation_states": dict(states),
        "document_relevance": dict(relevance),
        "status": "references_available" if accepted else "unmeasured",
        "quality_measured": False,
    }
    return report, references
