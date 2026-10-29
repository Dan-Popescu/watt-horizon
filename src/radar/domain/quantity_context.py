"""Evidence-backed measurements with their project/asset/phase context retained."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Asset = Literal["storage", "datacenter", "solar", "connection", "equipment", "mixed", "unknown"]
ProjectState = Literal["proposed", "existing", "example", "unknown"]


class QuantityObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    page: int = Field(ge=1)
    locator: str
    quote: str = Field(max_length=600)
    header_quote: str = Field(default="", max_length=600)
    low: float
    high: float
    dimension: Literal["power", "energy"]
    basis: str = ""
    asset: Asset
    state: ProjectState
    phase: str | None = None
    scenario: str | None = None
    source_kind: Literal["text", "table"]
    unit_in_cell: bool = True

    @property
    def eligible(self) -> bool:
        return (
            self.asset in {"storage", "datacenter"}
            and self.state == "proposed"
            and self.phase is None
            and self.scenario is None
            and self.basis not in {"dc", "c"}
            and self.unit_in_cell
        )
