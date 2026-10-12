"""Quality report against the gold set: screening recall, classifier agreement, field completeness."""

from dataclasses import dataclass, field

from radar.classification.gold import GoldLabel
from radar.domain.enums import TARGET_CATEGORIES, Category, DocumentRole
from radar.domain.models import Filing
from radar.domain.privacy import PersonScrubber
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database

TRACKED_FIELDS: tuple[str, ...] = ("developer", "siret", "commune", "filed_on", "power", "substation", "gps")


@dataclass
class Evaluation:
    gold_targets: int = 0
    screened_in: int = 0  # gold targets kept by layer 1 (recall numerator)
    classified: int = 0  # gold-labelled filings classified by a model (agreement denominator); 0 when offline
    agreements: int = 0
    missed: list[str] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    completeness: dict[str, str] = field(default_factory=dict)
    by_method: dict[str, int] = field(default_factory=dict)  # how every filing got its category

    @property
    def screening_recall(self) -> float | None:
        return self.screened_in / self.gold_targets if self.gold_targets else None


def dropped_by_screen(filing: Filing) -> bool:
    return filing.classification.method == "rules" and filing.classification.category is Category.OTHER


def evaluate(filings: list[Filing], gold: dict[str, GoldLabel]) -> Evaluation:
    ev = Evaluation()
    by_number = {f.number: f for f in filings}
    for number, gold_label in gold.items():
        if gold_label.category not in TARGET_CATEGORIES:
            continue
        ev.gold_targets += 1
        filing = by_number.get(number)
        if filing and not dropped_by_screen(filing):
            ev.screened_in += 1
        else:
            ev.missed.append(number)
    for filing in filings:
        method = filing.classification.method
        ev.by_method[method] = ev.by_method.get(method, 0) + 1
        label = gold.get(filing.number)
        if label is None or filing.classification.method in {"gold", "rules"}:
            continue
        ev.classified += 1
        if filing.classification.category is label.category:
            ev.agreements += 1
        else:
            ev.disagreements.append(f"{filing.number}: {filing.classification.category} != {label.category}")

    targets = [f for f in filings if f.classification.category in TARGET_CATEGORIES]
    for name in TRACKED_FIELDS:
        ev.completeness[name] = f"{sum(1 for f in targets if has_field(f, name))}/{len(targets)}"
    return ev


def has_field(filing: Filing, name: str) -> bool:
    values: dict[str, object] = {
        "developer": filing.developer,
        "siret": filing.siret,
        "commune": filing.location.commune,
        "filed_on": filing.filed_on,
        "power": filing.power.mw,
        "substation": filing.substation,
        "gps": filing.location.precision == "exact" or None,
    }
    return values[name] not in (None, "")


def audit_privacy(db: Database, blobs: BlobStore) -> list[str]:
    """Stored texts that still contain person data: names from form person fields, civility names, e-mails, phones.
    Expected result: an empty list."""
    leaks: list[str] = []
    for filing in db.filings():
        forms = [read_pdf(blobs.get(d.id)).fields for d in db.documents(filing.key) if d.role is DocumentRole.FORM]
        audit = PersonScrubber.for_filing(forms, [filing.developer, filing.legal_name])
        texts = {
            "title": filing.title,
            "description": filing.project_description or "",
            "classification": filing.classification.quote,
            **{f"evidence:{name}": ev.quote for name, ev in filing.evidence.items()},
        }
        leaks += [f"{filing.key} {where}" for where, text in texts.items() if audit.leaks(text)]
    return leaks
