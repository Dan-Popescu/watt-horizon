"""Domain models. Every extracted value is traceable: see `Evidence` and the `evidence` maps."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from radar.domain.enums import (
    Category,
    ChangeKind,
    CheckStatus,
    Confidence,
    DocumentRole,
    PowerStatus,
    ReviewDecision,
    Severity,
    SignalKind,
    SourceKind,
)


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(Model):
    """Where a value comes from. `locator` examples: 'field:Valeurs', 'clause:request', 'page:3', 'title'."""

    document_id: str
    locator: str
    quote: str = Field(max_length=600)


class Location(Model):
    commune: str | None = None
    department: str | None = None  # two-character code, e.g. "59", "2A"
    insee_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    precision: str = "none"  # "exact" (declared GPS), "commune" (commune centroid), "none"


class Power(Model):
    mw_min: float | None = None
    mw_max: float | None = None
    mwh: float | None = None
    status: PowerStatus = PowerStatus.UNKNOWN
    basis: str = ""  # how it was obtained, e.g. "field:Valeurs" or "estimate: 36 containers x 5.3 MWh"

    @property
    def mw(self) -> float | None:
        """Representative value (upper bound of the range)."""
        return self.mw_max if self.mw_max is not None else self.mw_min


class Check(Model):
    name: str
    status: CheckStatus
    detail: str


class Classification(Model):
    category: Category
    method: str  # "rules", "llm:<model>", "gold", "none"
    confidence: str = ""  # classifier-reported confidence, free text
    quote: str = ""  # justification quoted from the document
    quote_verified: bool | None = None  # True if the quote was found verbatim in the document


class DocumentRecord(Model):
    """A downloaded file. `id` is the SHA-256 of its bytes (content-addressed, deduplicated)."""

    id: str
    source: SourceKind
    url: str
    title: str
    role: DocumentRole
    filing_key: str
    fetched_at: datetime
    filename: str
    pages: int = 0
    scanned_pages: tuple[int, ...] = ()
    # Why a document the screen rejected was still fetched: "random" (unbiased recall estimate) or "near-miss:<why>".
    audit: str | None = None


class Flag(Model):
    """A detected reason to doubt a filing (see quality/flags.py for codes, labels and suggested fixes)."""

    code: str
    severity: Severity
    field: str | None = None
    detail: str = ""


class MachineOutput(Model):
    classification: Classification
    values: dict[str, str | float | None]  # see pipeline.corrections.field_values


class Filing(Model):
    """One administrative filing (e.g. a case-by-case request and its decision), possibly several documents."""

    key: str  # "<source>:<number>", e.g. "dreal_case_by_case:hauts-de-france:2025-8646"
    source: SourceKind
    region: str
    number: str
    title: str
    document_ids: tuple[str, ...]
    developer: str | None = None
    legal_name: str | None = None
    siret: str | None = None
    project_description: str | None = None
    review_decision: ReviewDecision = ReviewDecision.PENDING
    filed_on: date | None = None
    location: Location = Location()
    power: Power = Power()
    substation: str | None = None
    containers: int | None = None
    area_ha: float | None = None
    classification: Classification = Classification(category=Category.UNCLASSIFIED, method="none")
    checks: tuple[Check, ...] = ()
    confidence: Confidence = Confidence.REVIEW
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    unread_scans: tuple[str, ...] = ()  # document ids with scanned pages and no OCR text
    reading: dict[str, str] = Field(default_factory=dict)  # document id -> how it was read (text layer, OCR engine)
    versions: dict[str, str] = Field(default_factory=dict)  # extraction / classification / ocr versions
    # What the machine produced before human corrections: evaluation always measures this, never the corrections.
    machine: MachineOutput | None = None
    reviewed_at: datetime | None = None  # last human validation or correction
    flags: tuple[Flag, ...] = ()  # computed at every build: what a reviewer should look at first


class Correction(Model):
    """A human decision on one field of a filing. The latest active correction per field wins at every rebuild.
    `field` = "validated" means: every current value was checked and is right."""

    id: int | None = None
    filing_key: str
    field: str
    value: str | float | None  # None = absent from the documents
    note: str = ""
    author: str  # internal only: never exposed through the API or MCP
    created_at: datetime
    cause: str | None = None  # why the machine was wrong (see pipeline.corrections.CAUSES)
    blind: bool = False  # typed without seeing machine values: an unbiased measurement of the machine


class Signal(Model):
    """A weak, early clue from a non-procedural source (company register, press, Géorisques)."""

    key: str  # "<kind>:<stable id>"
    kind: SignalKind
    source: SourceKind
    title: str
    url: str
    observed_on: date | None = None
    developer: str | None = None
    communes: tuple[str, ...] = ()
    department: str | None = None
    category_hint: Category | None = None
    payload: dict[str, str] = Field(default_factory=dict)


class Link(Model):
    """A proposed relation between two records (filing or signal keys). A human confirms before merging."""

    a: str
    b: str
    reasons: tuple[str, ...]


class Project(Model):
    """What a client sees: one physical project, built from filings and enriched by linked signals."""

    id: str
    name: str
    category: Category
    developer: str | None
    location: Location
    power: Power
    substation: str | None
    status: str  # human-readable stage, e.g. "Cas par cas : non soumis à étude d'impact"
    first_seen: date | None
    updated_at: datetime
    confidence: Confidence
    filing_keys: tuple[str, ...]
    signal_keys: tuple[str, ...] = ()


class FieldChange(Model):
    field: str
    before: str | None
    after: str | None


class ProjectChange(Model):
    """One entry of the change log written at each project rebuild (what clients are alerted about)."""

    at: datetime
    kind: ChangeKind
    project_id: str
    name: str
    category: Category
    department: str | None
    fields: tuple[FieldChange, ...] = ()
