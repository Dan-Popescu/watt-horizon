"""View models returned to agents and humans. Field names carry units; every project links back to sources."""

from pydantic import BaseModel, ConfigDict, Field


class View(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProjectSummary(View):
    project_id: str
    name: str
    category: str
    developer: str | None
    commune: str | None
    department: str | None
    lat: float | None
    lon: float | None
    location_precision: str = Field(description="exact = GPS declared in the filing; commune = commune centroid")
    power_mw: float | None
    power_mw_min: float | None
    energy_mwh: float | None
    power_status: str = Field(description="known (read in a document), estimated (range from physical clues), unknown")
    substation: str | None
    status: str
    first_seen: str | None
    confidence: str = Field(description="high, medium, or review (not yet human-checked)")
    distance_km: float | None = None


class ProjectPage(View):
    items: list[ProjectSummary]
    next_cursor: str | None
    total: int
    known_power_mw: float
    declared_power_mw: float
    projects_without_power: int
    projects_to_review: int
    by_category: dict[str, int]


class EvidenceView(View):
    field: str
    quote: str
    locator: str
    document_url: str | None


class DocumentView(View):
    title: str
    url: str
    role: str
    read_by: str = Field(default="", description="text layer and/or OCR engine that read it (with pages)")


class CheckView(View):
    name: str
    status: str
    detail: str


class RelatedView(View):
    key: str
    kind: str
    title: str
    url: str | None
    reasons: list[str]


class ProjectDetail(View):
    summary: ProjectSummary
    description: str | None
    review_decision: str
    siret: str | None
    classification: str
    classification_method: str
    versions: dict[str, str] = Field(default_factory=dict, description="extraction / classification / OCR versions")
    power_basis: str
    documents: list[DocumentView]
    evidence: list[EvidenceView]
    checks: list[CheckView]
    related: list[RelatedView] = Field(
        description="Proposed links (same commune, developer or substation): unconfirmed"
    )


class ZoneSummary(View):
    lat: float
    lon: float
    radius_km: float
    project_count: int
    known_power_mw: float = Field(description="Sum of known or estimated upper-bound powers; unknown powers excluded")
    projects_without_power: int
    by_category: dict[str, int]
    projects: list[ProjectSummary]
    next_cursor: str | None = None


class DatasetInfo(View):
    counts: dict[str, int]
    last_runs: dict[str, str]
    coverage: str
    caveats: list[str]
    departments: list[str] = Field(default_factory=list)


class FieldChangeView(View):
    field: str
    label: str = Field(description="French label for humans")
    before: str | None
    after: str | None


class ChangeView(View):
    at: str = Field(description="UTC timestamp of the rebuild that detected the change")
    kind: str = Field(description="new, changed, or removed (no longer a target project)")
    project_id: str
    name: str
    category: str
    department: str | None
    summary: str = Field(description="one-line French summary for humans")
    fields: list[FieldChangeView]


class CorrectionView(View):
    id: int
    field: str
    value: str | float | None
    note: str
    author: str
    created_at: str
    cause: str | None = None
    blind: bool = False


class FlagView(View):
    code: str
    severity: str
    field: str | None
    detail: str
    label: str


class LinkSide(View):
    key: str
    kind: str  # filing | signal
    label: str  # filing number or signal title
    category: str | None
    developer: str | None
    commune: str | None
    power_mw: float | None
    substation: str | None
    date: str | None
    url: str | None


class LinkReviewItem(View):
    """A pair of records the linker related (internal review mode only)."""

    a: LinkSide
    b: LinkSide
    reasons: list[str]
    verdict: str | None
    merged_by_machine: bool
    priority: float


class ReviewItem(View):
    """One filing as a reviewer sees it (internal review mode only: includes reviewer names)."""

    key: str
    number: str
    title: str
    category: str
    machine_category: str
    machine_method: str
    draft_label: str | None = Field(description="unverified category label from the gold file, if any")
    confidence: str
    reviewed_at: str | None
    values: dict[str, str | float | None]
    machine_values: dict[str, str | float | None]
    evidence: dict[str, EvidenceView]
    documents: list[DocumentView]
    checks: list[CheckView]
    corrections: list[CorrectionView]
    priority: float = Field(default=0.0, description="review priority (0 = reviewed and up to date)")
    reasons: list[str] = Field(default_factory=list, description="why to review it, most important first")
    flags: list[FlagView] = Field(default_factory=list)
    stale_review: bool = False
    second_review: bool = False  # sampled for a blind second review (double annotation)
    field_confidence: dict[str, float] = Field(default_factory=dict, description="calibrated, per field")
    auto_accepted: list[str] = Field(default_factory=list, description="fields proven reliable enough to skip review")
    pending_fields: list[str] = Field(default_factory=list, description="fields still awaiting human approval")
    audit_fields: list[str] = Field(default_factory=list, description="auto-accepted fields sampled for human audit")
