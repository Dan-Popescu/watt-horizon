"""Validate explicit human observations; unknown or unfinished reviews never become absent gold."""

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf

from radar.domain.enums import Category, ReviewDecision
from radar.domain.fields import Value
from radar.domain.ids import luhn_ok
from radar.domain.quantities import NUMBER, UNIT, Measurement, measurements
from radar.domain.text import contains_quote, fold
from radar.evaluation.campaign import ANNOTATED_FIELDS, Annotation, Campaign, CampaignRecord, Observation
from radar.extraction.llm import value_in_quote
from radar.reading.pdf import (
    DEFAULT_MAX_TABLE_CHARS,
    DEFAULT_MAX_TABLE_PAGES,
    DEFAULT_MAX_TABLE_ROWS,
    PdfContent,
    PdfReader,
)
from radar.reading.tables import PdfTable, extract_tables
from radar.reading.workers import run_bounded
from radar.store.blobs import BlobStore

_TABLE_LOCATOR = re.compile(r"table:([1-9]\d{0,4}):row:([1-9]\d{0,4}):column:([1-9]\d{0,4})")
_HEADER_UNIT = re.compile(rf"(?<!\w)({UNIT})(?!\w)", re.IGNORECASE)
_CELL_NUMBER = re.compile(rf"\s*{NUMBER}(?:\s*(?:à|a|[-–])\s*{NUMBER})?\s*")


def _native_cited_tables(data: bytes, page: int, max_pages: int) -> tuple[PdfTable, ...]:
    """Inspect only the requested page, inside the existing terminable worker."""
    with pymupdf.open(stream=data, filetype="pdf") as document:  # type: ignore[no-untyped-call]
        if document.page_count > max_pages or page > document.page_count:
            raise ValueError("Cited table page exceeds document limits")
        tables, limited = extract_tables(
            document[page - 1], max_rows=DEFAULT_MAX_TABLE_ROWS, max_chars=DEFAULT_MAX_TABLE_CHARS
        )
        if limited:
            raise ValueError("Cited table exceeds row or character budget")
        return tables


def _table_value(name: str, value: str | float, quote: str, table: PdfTable, row: int, column: int) -> None:
    if row > len(table.rows) or column > len(table.rows[row - 1]):
        raise ValueError("Table citation row or column is outside the source table")
    cell = table.rows[row - 1][column - 1]
    if quote.strip() != cell.strip():
        raise ValueError("Table citation quote must equal the complete source cell")
    header = table.rows[0][column - 1] if column <= len(table.rows[0]) else ""
    if name not in {"power_mw", "power_mw_min", "energy_mwh"}:
        if name not in {"category", "review_decision", "document_relevance"} and not value_in_quote(
            name, value, header + " " + cell
        ):
            raise ValueError("Table annotation value is unsupported by its cell and column header")
        return
    if row == 1:
        raise ValueError("A quantity citation must identify a data row rather than the header")
    units = _HEADER_UNIT.findall(header)
    if len(units) > 1:
        raise ValueError("Table column header contains ambiguous units")
    values = measurements(cell)
    if not values:
        if len(units) != 1 or _CELL_NUMBER.fullmatch(cell) is None:
            raise ValueError("Table quantity requires a literal unit or an explicit unambiguous column unit")
        values = measurements(cell + " " + units[0])
    elif units:
        header_unit = measurements("1 " + units[0])[0]
        if header_unit.basis in {"dc", "c"}:
            raise ValueError("Table column unit has an incompatible power basis")
        inline_units = _HEADER_UNIT.findall(cell)
        for unit in inline_units:
            inline = measurements("1 " + unit)[0]
            if (
                inline.dimension != header_unit.dimension
                or inline.high != header_unit.high
                or (inline.basis and header_unit.basis and inline.basis != header_unit.basis)
            ):
                raise ValueError("Table cell unit contradicts its column header")
    dimension = "energy" if name == "energy_mwh" else "power"
    candidates: list[Measurement] = [measurement for measurement in values if measurement.dimension == dimension]
    if len(candidates) != 1 or any(measurement.basis in {"dc", "c"} for measurement in candidates):
        raise ValueError("Table quantity has incompatible dimension, basis or multiple values")
    candidate = candidates[0]
    if name == "energy_mwh" and candidate.low != candidate.high:
        raise ValueError("An energy range cannot establish one scalar capacity")
    expected = candidate.low if name == "power_mw_min" else candidate.high
    if not isinstance(value, int | float) or not math.isclose(float(value), expected, rel_tol=1e-8, abs_tol=1e-10):
        raise ValueError("Table quantity does not match the normalized source value")


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
    table_content: Callable[[str, int], tuple[PdfTable, ...]],
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
    if evidence.locator.startswith("table:"):
        locator = _TABLE_LOCATOR.fullmatch(evidence.locator)
        if locator is None or evidence.page is None or evidence.page > pdf.pages:
            raise ValueError("Table citation needs a valid page and table:INDEX:row:ROW:column:COLUMN locator")
        index, row_index, column = map(int, locator.groups())
        table = next((t for t in table_content(evidence.document_id, evidence.page) if t.index == index), None)
        if table is None:
            raise ValueError("Cited table was not found on the specified source page")
        _table_value(name, observation.value, evidence.quote, table, row_index, column)
        return
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
    table_cache: dict[tuple[str, int], tuple[PdfTable, ...]] = {}

    def content(document_id: str) -> PdfContent:
        if document_id not in cache:
            data = blobs.get(document_id, max_bytes=reader.max_bytes)
            if hashlib.sha256(data).hexdigest() != document_id:
                raise ValueError("Campaign document bytes do not match their hash")
            cache[document_id] = (read or reader.read)(document_id, data)
        return cache[document_id]

    def table_content(document_id: str, page: int) -> tuple[PdfTable, ...]:
        available = tuple(table for table in content(document_id).tables if table.page == page)
        if available:
            return available
        key = (document_id, page)
        if key not in table_cache:
            if sum(key[0] == document_id for key in table_cache) >= DEFAULT_MAX_TABLE_PAGES:
                raise ValueError("Table citation page budget exceeded")
            data = blobs.get(document_id, max_bytes=reader.max_bytes)
            if hashlib.sha256(data).hexdigest() != document_id:
                raise ValueError("Campaign document bytes do not match their hash")
            _, result = next(
                run_bounded(
                    _native_cited_tables,
                    [(document_id, (data, page, reader.max_pages))],
                    max_workers=1,
                    timeout_s=reader.document_timeout_s,
                )
            )
            if result.error or result.value is None:
                raise ValueError("Table citation could not be verified: " + (result.error or "no worker result"))
            table_cache[key] = result.value
        return table_cache[key]

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
            # Absence claims still require the original bytes to be available and
            # readable. A declared inventory cannot stand in for missing evidence.
            for document in record.documents:
                content(document.id)
            if any(
                old.reviewer.casefold().strip() == row.reviewer.casefold().strip() for old in reviews[row.filing_key]
            ):
                raise ValueError("Duplicate reviewer for a campaign filing")
            for name, observation in row.fields.items():
                _observation(name, observation, row, record, content, table_content)
                states[observation.status] += 1
            for document_id, observation in row.document_relevance.items():
                _observation("document_relevance", observation, row, record, content, table_content)
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
