"""Builds one `Filing` from its documents: read -> extract -> screen/classify -> validate -> confidence."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from radar.classification.base import ClassificationInput, Classifier
from radar.classification.rules import screen
from radar.domain.departments import code_from_postcode
from radar.domain.enums import TARGET_CATEGORIES, Category, CheckStatus, DocumentRole, PowerStatus, ReviewDecision
from radar.domain.fields import field_values
from radar.domain.models import (
    Candidate,
    Classification,
    Correction,
    DocumentRecord,
    Evidence,
    Filing,
    Location,
    Power,
)
from radar.domain.power import estimate_power
from radar.domain.privacy import PersonScrubber
from radar.domain.text import contains_quote, squash
from radar.extraction import EXTRACTION_VERSION
from radar.extraction.cerfa import extract_cerfa, form_layout
from radar.extraction.decision import extract_decision
from radar.extraction.facts import Facts
from radar.extraction.llm import ExtractionInput, FieldExtractor
from radar.extraction.substation import is_voltage_only
from radar.pipeline.corrections import apply_corrections, with_machine
from radar.quality.arbiter import Arbiter, comparable
from radar.quality.confidence import Calibration, raw_scores
from radar.quality.flags import QualityInput, ReadDocument, detect
from radar.quality.propagate import ALIAS_FIELDS, Aliases
from radar.reading.ocr import OcrEngine
from radar.reading.pdf import PdfContent, read_pdf
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
    extractor: FieldExtractor | None = None  # LLM fallback for fields the rules missed (target filings only)
    arbiter: Arbiter | None = None  # learned source reliabilities (None: priors = hand-written precedence)
    calibration: Calibration | None = None  # raw field score -> observed accuracy (None: raw scores, no auto-accept)
    aliases: Aliases | None = None  # learned spellings of names (gold/aliases.json), applied to every build


def read_documents(documents: list[DocumentRecord], services: Services) -> list[PdfContent]:
    """Text layer + form fields + OCR for each document, in order."""
    contents = []
    for doc in documents:
        data = services.blobs.get(doc.id)
        contents.append(services.ocr.complete(doc.id, data, read_pdf(data)))
    return contents


def classification_text(description: str | None, texts: list[str]) -> str:
    """What a classifier sees: the project description first, then every document (already GDPR-scrubbed)."""
    return squash(" ".join([description or "", *texts]))


# Field -> (Facts attribute, evidence key) for arbitrated fields.
ARBITRATED_ATTRS: dict[str, tuple[str, str]] = {
    "developer": ("developer", "developer"),
    "siret": ("siret", "siret"),
    "commune": ("commune", "commune"),
    "power_mw": ("mw_max", "power"),
    "substation": ("substation", "substation"),
    "filed_on": ("filed_on", "filed_on"),
    "review_decision": ("review_decision", "review_decision"),
}


@dataclass
class Extraction:
    form: Facts
    decision: Facts
    facts: Facts  # merged, arbitrated
    candidates: dict[str, list[Candidate]]
    support: dict[str, tuple[float, int, int]]  # field -> (support of the chosen value, agreeing sources, candidates)


def _candidates(role: str, facts: Facts) -> dict[str, Candidate]:
    out = {}
    for name, (attr, key) in ARBITRATED_ATTRS.items():
        value = getattr(facts, attr)
        if name == "power_mw" and value is None:
            value = facts.mw_min
        if value in (None, ""):
            continue
        ev = facts.evidence.get(key)
        kind = ev.locator.split(":")[0] if ev else "unknown"
        weak = name == "substation" and is_voltage_only(str(value))
        out[name] = Candidate(source=f"{role}:{kind}", value=comparable(value), weak=weak)
    return out


def _take(facts: Facts, winner: Facts, other: Facts, name: str) -> None:
    attr, key = ARBITRATED_ATTRS[name]
    if name == "power_mw":
        facts.mw_min, facts.mw_max, facts.mwh = winner.mw_min, winner.mw_max, winner.mwh
        if facts.mwh is None and other.mwh is not None and other.mw_max in (None, winner.mw_max):
            facts.mwh = other.mwh  # same power, energy stated only in the other document
    else:
        setattr(facts, attr, getattr(winner, attr))
    if key in winner.evidence:
        facts.evidence[key] = winner.evidence[key]


def extract_facts(
    documents: list[tuple[str, DocumentRole, str]], contents: list[PdfContent], arbiter: Arbiter | None = None
) -> Extraction:
    """Rule extraction for one filing, then arbitration between form and decision values (quality/arbiter.py).
    Pure: also used to replay regression cases from stored texts. `documents` = (id, role, title) per content."""
    form, decision = Facts(), Facts()
    for (doc_id, role, title), content in zip(documents, contents, strict=True):
        if role is DocumentRole.FORM:
            form.fill_from(extract_cerfa(doc_id, content))
        elif role is DocumentRole.DECISION:
            decision.fill_from(extract_decision(doc_id, content.text, title))
    facts = Facts()
    facts.fill_from(form)
    facts.fill_from(decision, override=DECISION_WINS)
    by_role = {"form": _candidates("form", form), "decision": _candidates("decision", decision)}
    source_facts = {"form": form, "decision": decision}
    arbiter = arbiter or Arbiter()
    candidates: dict[str, list[Candidate]] = {}
    support: dict[str, tuple[float, int, int]] = {}
    for name in ARBITRATED_ATTRS:
        options = [by_role[r][name] for r in ("form", "decision") if name in by_role[r]]
        if not options:
            continue
        candidates[name] = options
        if chosen := arbiter.choose(name, options):
            lead, score, agreeing = chosen
            winner = lead.source.split(":")[0]
            _take(facts, source_facts[winner], source_facts["decision" if winner == "form" else "form"], name)
            support[name] = (score, agreeing, len(options))
    titles = [title for _, _, title in documents]
    facts.department = facts.department or code_from_postcode(facts.postcode) or _department_from_titles(titles)
    return Extraction(form, decision, facts, candidates, support)


def build_filing(
    key: str, documents: list[DocumentRecord], services: Services, corrections: Sequence[Correction] = ()
) -> Filing:
    """Machine output (kept in `Filing.machine`) with human corrections applied on top."""
    if not documents:
        raise ValueError(f"filing {key} has no documents")
    source, region, number = documents[0].source, *key.split(":", 2)[1:]
    contents = read_documents(documents, services)
    texts = [c.text for c in contents]
    form_fields = [c.fields for c in contents]
    reading = {doc.id: c.read_by for doc, c in zip(documents, contents, strict=True)}
    unread = [
        doc.id for doc, c in zip(documents, contents, strict=True) if c.unread_scanned_pages and c.is_mostly_scanned
    ]

    extraction = extract_facts([(d.id, d.role, d.title) for d in documents], contents, services.arbiter)
    form, decision, facts = extraction.form, extraction.decision, extraction.facts
    if services.aliases is not None:  # « la SAS X » -> « X »: verified spellings learned from corrections
        for name in ALIAS_FIELDS:
            setattr(facts, name, services.aliases.apply(name, getattr(facts, name)))
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
    extractor_used = None
    if services.extractor and classification.category in TARGET_CATEGORIES:
        extractor_used = _llm_fallback(key, title, facts, documents, texts, services.extractor)
        for name in ARBITRATED_ATTRS:  # LLM values are candidates too: their reliability is learned like any source
            ev = facts.evidence.get(ARBITRATED_ATTRS[name][1])
            if ev is not None and ev.locator.startswith("llm-extract:"):
                value = getattr(facts, ARBITRATED_ATTRS[name][0])
                extraction.candidates.setdefault(name, []).append(Candidate(source="llm", value=comparable(value)))
    validation = validate(facts, services.lookups)
    flags = detect(
        QualityInput(
            documents=[
                ReadDocument(
                    id=doc.id,
                    role=doc.role,
                    text=text,
                    fields=content.fields,
                    layout=form_layout(content.fields) if doc.role is DocumentRole.FORM else None,
                    ocr_texts=content.ocr_texts,
                    ocr_engines=content.ocr_engines,
                    audit=doc.audit,
                )
                for doc, content, text in zip(documents, contents, texts, strict=True)
            ],
            form=form,
            decision=decision,
            facts=facts,
            classification=classification,
            checks=validation.checks,
            target=classification.category in TARGET_CATEGORIES,
            kept_by_text=screen(title, facts.description, *texts).keep,
            unread=unread,
        )
    )
    gps_ok = any(c.name == "gps" and c.status is CheckStatus.OK for c in validation.checks)
    needs_review = (
        bool(unread)
        or classification.category in {Category.NEEDS_REVIEW, Category.UNCLASSIFIED}
        or classification.quote_verified is False
    )

    machine = Filing(
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
        reading=reading,
        flags=flags,
        candidates=extraction.candidates,
        strata=_strata(source.value, documents, contents, unread, facts.department),
        versions={
            "extraction": EXTRACTION_VERSION,
            "classification": classification.method,
            "ocr": services.ocr.label,
            **({"extraction_llm": extractor_used} if extractor_used else {}),
        },
    )
    raw = raw_scores(field_values(machine), extraction.support, flags, validation.checks, machine.strata["read"])
    calibration = services.calibration or Calibration()
    machine = machine.model_copy(
        update={
            "field_score": raw,
            "field_confidence": {name: calibration.confidence(score) for name, score in raw.items()},
            "auto_accepted": tuple(name for name, score in raw.items() if calibration.auto_accepted(score)),
        }
    )
    texts_by_doc = {doc.id: text for doc, text in zip(documents, texts, strict=True)}
    return apply_corrections(with_machine(machine), corrections, services.lookups, texts_by_doc)


def _strata(
    source: str, documents: list[DocumentRecord], contents: list[PdfContent], unread: list[str], department: str | None
) -> dict[str, str]:
    """Slices used to break metrics and errors down (a global score hides a weak document type)."""
    layouts = sorted(
        {form_layout(c.fields) for d, c in zip(documents, contents, strict=True) if d.role is DocumentRole.FORM}
    )
    engines = {e for c in contents for e in c.ocr_engines.values()}
    if unread:
        read = "unread"
    elif any(e != "transcript" for e in engines):
        read = "ocr"
    elif engines:
        read = "transcript"
    else:
        read = "text"
    return {
        "source": source,
        "form_layout": "+".join(layouts) or "no-form",
        "read": read,
        "department": department or "?",
    }


# Facts attribute for each fallback field.
FALLBACK_FIELDS: dict[str, str] = {
    "developer": "developer",
    "siret": "siret",
    "commune": "commune",
    "power_mw": "mw_max",
    "power_mw_min": "mw_min",
    "energy_mwh": "mwh",
    "substation": "substation",
    "filed_on": "filed_on",
}


def _llm_fallback(
    key: str, title: str, facts: Facts, documents: list[DocumentRecord], texts: list[str], extractor: FieldExtractor
) -> str | None:
    """Ask the LLM only for missing fields; keep verified answers with their quote as evidence."""
    missing = tuple(name for name, attr in FALLBACK_FIELDS.items() if getattr(facts, attr) in (None, ""))
    if facts.mw_max is not None:
        missing = tuple(m for m in missing if m != "power_mw_min")  # a single declared power is not a range
    if not missing:
        return None
    found = extractor.extract(ExtractionInput(key, title, classification_text(facts.description, texts), missing))
    for name, (value, quote) in found.items():
        doc = next((d.id for d, t in zip(documents, texts, strict=True) if contains_quote(t, quote)), documents[0].id)
        evidence = Evidence(document_id=doc, locator=extractor.name, quote=quote)
        typed = date.fromisoformat(str(value)) if name == "filed_on" else value
        if name in {"power_mw", "power_mw_min", "energy_mwh"}:
            setattr(facts, FALLBACK_FIELDS[name], typed)
            facts.evidence.setdefault("power", evidence)
        else:
            facts.set(FALLBACK_FIELDS[name], typed, evidence)
    return extractor.name


def _classify(
    key: str, number: str, title: str, facts: Facts, texts: list[str], classifier: Classifier | None, has_unread: bool
) -> Classification:
    screening = screen(title, facts.description, *texts)
    if not screening.keep:
        return Classification(category=Category.OTHER, method="rules", confidence="no clue")
    if classifier is not None:
        text = classification_text(facts.description, texts)
        if result := classifier.classify(ClassificationInput(filing_key=key, number=number, title=title, text=text)):
            return result
    category = Category.NEEDS_REVIEW if has_unread else Category.UNCLASSIFIED
    return Classification(category=category, method="rules", confidence="clues: " + ", ".join(screening.clues[:6]))


def _best_title(documents: list[DocumentRecord], facts: Facts) -> str:
    for role in (DocumentRole.DECISION, DocumentRole.FORM, DocumentRole.OPINION, DocumentRole.OTHER):
        if doc := next((d for d in documents if d.role is role and d.title), None):
            return squash(doc.title)
    return facts.title or ""


def _department_from_titles(titles: list[str]) -> str | None:
    for title in titles:
        if m := re.search(r"\((\d{2}|2[AB])\)", title):
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
