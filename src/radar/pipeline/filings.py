"""Builds one `Filing` from its documents: read -> extract -> screen/classify -> validate -> confidence."""

import re
from dataclasses import dataclass

from radar.classification.base import ClassificationInput, Classifier
from radar.classification.rules import screen
from radar.domain.departments import code_from_postcode
from radar.domain.enums import Category, CheckStatus, DocumentRole, PowerStatus, ReviewDecision
from radar.domain.models import Classification, DocumentRecord, Filing, Location, Power
from radar.domain.power import estimate_power
from radar.domain.privacy import PersonScrubber
from radar.domain.text import squash
from radar.extraction.cerfa import extract_cerfa
from radar.extraction.decision import extract_decision
from radar.extraction.facts import Facts
from radar.extraction.substation import is_voltage_only
from radar.reading.ocr import OcrEngine
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore
from radar.validation.lookups import CommuneInfo, Lookups
from radar.validation.validator import validate

# The decision is the official act: its reading of these fields wins over the request form.
DECISION_WINS = frozenset({"review_decision", "commune", "department"})


@dataclass(frozen=True)
class Services:
    blobs: BlobStore
    ocr: OcrEngine
    classifier: Classifier | None
    lookups: Lookups


def build_filing(key: str, documents: list[DocumentRecord], services: Services) -> Filing:
    if not documents:
        raise ValueError(f"filing {key} has no documents")
    source, region, number = documents[0].source, *key.split(":", 2)[1:]
    form, decision, unread = Facts(), Facts(), []
    texts: list[str] = []
    form_fields: list[dict[str, str]] = []

    for doc in documents:
        data = services.blobs.get(doc.id)
        content = services.ocr.complete(doc.id, data, read_pdf(data))
        if content.unread_scanned_pages and content.is_mostly_scanned:
            unread.append(doc.id)
        texts.append(content.text)
        form_fields.append(content.fields)
        if doc.role is DocumentRole.FORM:
            form.fill_from(extract_cerfa(doc.id, content))
        elif doc.role is DocumentRole.DECISION:
            decision.fill_from(extract_decision(doc.id, content.text, doc.title))

    facts = Facts()
    facts.fill_from(form)
    facts.fill_from(decision, override=DECISION_WINS)
    if is_voltage_only(facts.substation) and decision.substation and not is_voltage_only(decision.substation):
        facts.evidence.pop("substation", None)
        facts.set("substation", decision.substation, decision.evidence.get("substation"))
    facts.department = facts.department or code_from_postcode(facts.postcode) or _department_from_titles(documents)
    # GDPR: from here on, nothing derived from the documents may carry a natural person's name.
    privacy = PersonScrubber.for_filing(form_fields, [facts.developer, facts.legal_name])
    texts = [privacy.scrub(t) or "" for t in texts]
    facts.description = privacy.scrub(facts.description)
    facts.evidence = {
        name: ev.model_copy(update={"quote": privacy.scrub(ev.quote)}) for name, ev in facts.evidence.items()
    }
    title = privacy.scrub(_best_title(documents, facts)) or ""

    classification = _classify(key, number, title, facts, texts, services.classifier, bool(unread))
    classification = classification.model_copy(update={"quote": privacy.scrub(classification.quote) or ""})
    validation = validate(facts, services.lookups)
    gps_ok = any(c.name == "gps" and c.status is CheckStatus.OK for c in validation.checks)
    needs_review = (
        bool(unread)
        or classification.category in {Category.NEEDS_REVIEW, Category.UNCLASSIFIED}
        or classification.quote_verified is False
    )

    return Filing(
        key=key,
        source=source,
        region=region,
        number=number,
        title=title,
        document_ids=tuple(d.id for d in documents),
        developer=facts.developer,
        legal_name=facts.legal_name,
        siret=facts.siret,
        project_description=facts.description,
        review_decision=facts.review_decision or ReviewDecision.PENDING,
        filed_on=facts.filed_on,
        location=_location(facts, validation.commune, gps_ok),
        power=_power(facts),
        substation=facts.substation,
        containers=facts.containers,
        area_ha=facts.area_ha,
        classification=classification,
        checks=validation.checks,
        confidence=validation.confidence(needs_review),
        evidence=dict(facts.evidence),
        unread_scans=tuple(unread),
    )


def _classify(
    key: str, number: str, title: str, facts: Facts, texts: list[str], classifier: Classifier | None, has_unread: bool
) -> Classification:
    screening = screen(title, facts.description, *texts)
    if not screening.keep:
        return Classification(category=Category.OTHER, method="rules", confidence="no clue")
    if classifier is not None:
        text = squash(" ".join([facts.description or "", *texts]))
        if result := classifier.classify(ClassificationInput(filing_key=key, number=number, title=title, text=text)):
            return result
    category = Category.NEEDS_REVIEW if has_unread else Category.UNCLASSIFIED
    return Classification(category=category, method="rules", confidence="clues: " + ", ".join(screening.clues[:6]))


def _best_title(documents: list[DocumentRecord], facts: Facts) -> str:
    for role in (DocumentRole.DECISION, DocumentRole.FORM, DocumentRole.OPINION, DocumentRole.OTHER):
        if doc := next((d for d in documents if d.role is role and d.title), None):
            return squash(doc.title)
    return facts.title or ""


def _department_from_titles(documents: list[DocumentRecord]) -> str | None:
    for doc in documents:
        if m := re.search(r"\((\d{2}|2[AB])\)", doc.title):
            return m[1]
    return None


def _location(facts: Facts, info: CommuneInfo | None, gps_ok: bool) -> Location:
    lat: float | None
    lon: float | None
    if gps_ok and facts.lat is not None and facts.lon is not None:
        lat, lon, precision = facts.lat, facts.lon, "exact"
    elif info and info.lat is not None and info.lon is not None:
        lat, lon, precision = info.lat, info.lon, "commune"
    else:
        lat = lon = None
        precision = "none"
    return Location(
        commune=info.name if info else facts.commune,
        department=info.department if info else facts.department,
        insee_code=info.insee_code if info else None,
        lat=lat,
        lon=lon,
        precision=precision,
    )


def _power(facts: Facts) -> Power:
    if facts.mw_max is not None or facts.mwh is not None:
        basis = facts.evidence["power"].locator if "power" in facts.evidence else "document"
        return Power(mw_min=facts.mw_min, mw_max=facts.mw_max, mwh=facts.mwh, status=PowerStatus.KNOWN, basis=basis)
    return estimate_power(facts.containers, facts.area_ha) or Power()
