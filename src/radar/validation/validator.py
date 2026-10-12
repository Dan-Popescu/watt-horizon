"""Runs all checks on a filing's facts and derives its publication confidence."""

from dataclasses import dataclass

from radar.domain.enums import CheckStatus, Confidence
from radar.domain.models import Check
from radar.extraction.facts import Facts
from radar.validation.checks import (
    check_commune,
    check_dates,
    check_gps,
    check_natural_person,
    check_power,
    check_siret,
)
from radar.validation.lookups import CommuneInfo, Lookups


@dataclass(frozen=True)
class Validation:
    checks: tuple[Check, ...]
    commune: CommuneInfo | None

    @property
    def has_alert(self) -> bool:
        return any(c.status is CheckStatus.ALERT for c in self.checks)

    def confidence(self, needs_review: bool) -> Confidence:
        if needs_review or self.has_alert:
            return Confidence.REVIEW
        unknown = sum(1 for c in self.checks if c.status is CheckStatus.UNKNOWN)
        return Confidence.HIGH if unknown <= 1 else Confidence.MEDIUM


def validate(facts: Facts, lookups: Lookups) -> Validation:
    """Mutates `facts` only for GDPR masking (see `check_natural_person`)."""
    gdpr = check_natural_person(facts)
    commune_check, commune = check_commune(facts, lookups)
    checks = (
        gdpr,
        check_siret(facts, lookups),
        commune_check,
        check_gps(facts, commune, lookups),
        check_power(facts),
        check_dates(facts),
    )
    return Validation(checks=checks, commune=commune)
