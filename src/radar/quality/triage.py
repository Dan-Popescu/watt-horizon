"""Review queue: which filings a human should look at first, and why. Pure function over stored filings.

Priority = (doubt + 0.5) x stakes + audit duty + stale review, where doubt = sum of flag severities, stakes =
1 (+1 tracked project, + log of the power, +0.5 recent filing). Unflagged filings stay in the queue (they still
need a human check to become references) but come last. Random audit items must all be labelled, or the recall
estimate is biased. Reviewed filings leave the queue, unless the machine output changed since the validation
snapshot ("stale review").
"""

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from radar.domain.enums import TARGET_CATEGORIES, Severity
from radar.domain.fields import machine_values
from radar.domain.models import Correction, Filing
from radar.evaluation.fields import same
from radar.pipeline.corrections import VALIDATED, latest
from radar.quality.flags import FLAGS

POINTS = {Severity.ERROR: 5.0, Severity.WARNING: 2.0, Severity.INFO: 0.5}
BASE_DOUBT = 0.5
AUDIT_RANDOM_BONUS = 6.0
STALE_BONUS = 6.0
RECENT_DAYS = 120
UNCERTAIN = 0.9  # calibrated field confidence below this adds doubt
LOW_CONFIDENCE = 0.6  # ... and below this, the field is named in the reasons
UNCERTAINTY_WEIGHT = 2.0
CHANGED_WEIGHT = 2.0  # per machine value changed by a new version and not reviewed since


@dataclass
class TriageItem:
    key: str
    number: str
    title: str
    priority: float
    reasons: list[str] = field(default_factory=list)  # French, most important first
    flag_codes: list[str] = field(default_factory=list)
    stale_review: bool = False


def stale_fields(filing: Filing, validation: Correction | None) -> list[str]:
    """Fields whose machine value changed since the human validated the filing."""
    if validation is None or not isinstance(validation.value, str):
        return []
    try:
        snapshot = json.loads(validation.value)
    except ValueError:
        return []
    now = machine_values(filing)
    return sorted(k for k in now if k in snapshot and snapshot[k] != now[k])


def triage(
    filings: Iterable[Filing],
    corrections: Iterable[Correction],
    today: date,
    machine_changes: dict[str, list[dict[str, object]]] | None = None,
) -> list[TriageItem]:
    """`machine_changes` (db.machine_changes()): outputs changed by a new version and not reviewed since."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    queue = []
    for f in filings:
        reviewed = latest(by_key.get(f.key, []))
        stale = stale_fields(f, reviewed.get(VALIDATED))
        if reviewed and not stale:
            continue
        item = TriageItem(f.key, f.number, f.title, 0.0, flag_codes=sorted({g.code for g in f.flags}))
        doubt = BASE_DOUBT
        for g in f.flags:
            doubt += POINTS[g.severity]
            label = FLAGS[g.code].label_fr + (f" ({g.field})" if g.field else "")
            item.reasons.append(f"{label} : {g.detail}" if g.detail else label)
        if f.classification.category in TARGET_CATEGORIES:
            uncertain = {
                name: c for name, c in f.field_confidence.items() if c < UNCERTAIN and name not in f.auto_accepted
            }
            doubt += sum(UNCERTAIN - c for c in uncertain.values()) * UNCERTAINTY_WEIGHT
            if low := sorted((c, n) for n, c in uncertain.items() if c < LOW_CONFIDENCE):
                item.reasons.append("champs incertains : " + ", ".join(f"{n} ({c:.0%})" for c, n in low))
        if changed := changed_since_review(f, (machine_changes or {}).get(f.key, [])):
            doubt += CHANGED_WEIGHT * len(changed)
            item.reasons.insert(0, "modifié par une nouvelle version : " + " ; ".join(changed))
        stakes = 1.0
        if f.classification.category in TARGET_CATEGORIES:
            stakes += 1.0 + math.log1p(f.power.mw or 0) / 4
        if f.filed_on and (today - f.filed_on).days <= RECENT_DAYS:
            stakes += 0.5
        item.priority = doubt * stakes
        if any(g.code == "audit_sample" and g.detail == "random" for g in f.flags):
            item.priority += AUDIT_RANDOM_BONUS
            item.reasons.insert(0, "échantillon aléatoire : à étiqueter pour mesurer ce que le tri rate")
        if stale:
            item.stale_review = True
            item.priority += STALE_BONUS
            item.reasons.insert(0, "valeurs modifiées depuis la relecture : " + ", ".join(stale))
        queue.append(item)
    return sorted(queue, key=lambda i: (-i.priority, i.number))


def changed_since_review(filing: Filing, changes: list[dict[str, object]]) -> list[str]:
    """Human-readable machine changes newer than the filing's last review (all of them if never reviewed)."""
    reviewed = filing.reviewed_at.isoformat() if filing.reviewed_at else ""
    current = machine_values(filing)
    return [
        f"{c['field']} : {c['before'] if c['before'] is not None else '—'} → {c['after'] if c['after'] is not None else '—'}"
        for c in changes
        if str(c["at"]) > reviewed and same_or_none(current.get(str(c["field"])), c["after"])
    ]


def same_or_none(a: object, b: object) -> bool:
    """The recorded change is still the current value (a later change supersedes it)."""
    if a in (None, "") or b in (None, ""):
        return a in (None, "") and b in (None, "")
    return same(a, b)  # type: ignore[arg-type]
