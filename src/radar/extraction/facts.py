"""`Facts`: extracted values with their evidence, mergeable across the documents of one filing."""

from dataclasses import dataclass, field, fields
from datetime import date

from radar.domain.enums import ReviewDecision
from radar.domain.models import Evidence


@dataclass
class Facts:
    number: str | None = None
    title: str | None = None
    developer: str | None = None
    legal_name: str | None = None
    siret: str | None = None
    description: str | None = None
    procedures: str | None = None
    commune: str | None = None
    department: str | None = None
    postcode: str | None = None
    lat: float | None = None
    lon: float | None = None
    mw_min: float | None = None
    mw_max: float | None = None
    mwh: float | None = None
    containers: int | None = None
    area_ha: float | None = None
    substation: str | None = None
    filed_on: date | None = None
    review_decision: ReviewDecision | None = None
    evidence: dict[str, Evidence] = field(default_factory=dict)

    def set(self, name: str, value: object, evidence: Evidence | None = None) -> None:
        """Set a value only if it is not empty; attach its evidence."""
        if value is None or value == "":
            return
        setattr(self, name, value)
        if evidence is not None:
            self.evidence[name] = evidence

    def fill_from(self, other: "Facts", override: frozenset[str] = frozenset()) -> None:
        """Copy values missing here from `other`; names in `override` are always taken from `other`."""
        for f in fields(self):
            if f.name == "evidence":
                continue
            theirs = getattr(other, f.name)
            if theirs is None:
                continue
            if getattr(self, f.name) is None or f.name in override:
                setattr(self, f.name, theirs)
                if f.name in other.evidence:
                    self.evidence[f.name] = other.evidence[f.name]
        # Power values travel together with one evidence entry named "power".
        if "power" in other.evidence and ("power" not in self.evidence or "mw_max" in override):
            self.evidence["power"] = other.evidence["power"]
