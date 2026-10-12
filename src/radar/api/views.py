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


class EvidenceView(View):
    field: str
    quote: str
    locator: str
    document_url: str | None


class DocumentView(View):
    title: str
    url: str
    role: str


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


class DatasetInfo(View):
    counts: dict[str, int]
    last_runs: dict[str, str]
    coverage: str
    caveats: list[str]


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
