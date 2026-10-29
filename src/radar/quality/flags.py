"""Doubt detectors: computed at every filing build, without any reference. Each flag says WHY a filing deserves a
look and, for an agent, WHAT to improve. Adding a detector = one function below + one `FLAGS` entry + a test.

Flags feed the review queue (quality/triage.py); their usefulness is measured afterwards (quality/errors.py:
share of flagged filings that were really corrected, and errors no flag predicted).
"""

import re
from dataclasses import dataclass, field

from radar.classification.bayes import CONFIDENT, Opinion
from radar.domain.enums import CheckStatus, DocumentRole, Severity
from radar.domain.ids import luhn_ok
from radar.domain.models import Check, Classification, Flag
from radar.domain.text import contains_quote, fold
from radar.extraction.facts import Facts


@dataclass(frozen=True)
class FlagInfo:
    severity: Severity
    label_fr: str  # shown to reviewers
    action: str  # for agents: where to look / what to change


FLAGS: dict[str, FlagInfo] = {
    "unread_scan": FlagInfo(
        Severity.ERROR, "pages scannées non lues", "add a transcript or an OCR engine (reading/ocr.py)"
    ),
    "ocr_noisy": FlagInfo(
        Severity.WARNING, "OCR bruité", "check the OCR text; consider a better engine or a transcript"
    ),
    "reading_incomplete": FlagInfo(
        Severity.WARNING,
        "lecture documentaire incomplète",
        "inspect unread tables or OCR errors and retry within resource limits",
    ),
    "quantity_context_review": FlagInfo(
        Severity.WARNING,
        "quantités à contextualiser",
        "review project phase, scenario, asset and unit basis before accepting a scalar",
    ),
    "form_layout_unknown": FlagInfo(
        Severity.ERROR,
        "mise en page de formulaire inconnue",
        "map the new field names in extraction/cerfa.FIELD_ALIASES",
    ),
    "new_template": FlagInfo(
        Severity.WARNING,
        "modèle de document inconnu",
        "untested layout: check its extraction, add rules or field aliases, then radar templates export",
    ),
    "form_flattened": FlagInfo(
        Severity.INFO, "formulaire aplati (lu par le texte)", "text fallback in extraction/cerfa.py"
    ),
    "quote_not_found": FlagInfo(
        Severity.ERROR, "citation introuvable dans le document", "fix the extractor building this evidence quote"
    ),
    "disagreement": FlagInfo(
        Severity.WARNING, "formulaire et décision divergent", "check which document is right; fix the wrong extractor"
    ),
    "mentioned_not_extracted": FlagInfo(
        Severity.WARNING, "mentionné dans le texte mais non extrait", "extend the rule (extraction/ or domain/power.py)"
    ),
    "missing": FlagInfo(
        Severity.WARNING, "champ manquant sur un projet suivi", "absent from documents, or an extractor gap"
    ),
    "check_alert": FlagInfo(
        Severity.WARNING, "contrôle automatique en alerte", "validation/checks.py explains the alert"
    ),
    "llm_value": FlagInfo(Severity.INFO, "valeur trouvée par le LLM", "sample-check; if right, consider a rule"),
    "classification_unverified": FlagInfo(
        Severity.WARNING, "justification du classement introuvable", "classifier quote not in the text: doubt the label"
    ),
    "category_undecided": FlagInfo(Severity.ERROR, "catégorie non décidée", "classify by hand; add a gold label"),
    "rescued_by_text": FlagInfo(
        Severity.ERROR,
        "rejeté sur son titre, retenu sur son texte",
        "the title screen missed it: add the pattern to classification/rules.py (recall bug)",
    ),
    "second_opinion_disagrees": FlagInfo(
        Severity.WARNING,
        "le classifieur local propose une autre catégorie",
        "check the category; if the local model is right, the classifier or the screen needs a fix",
    ),
    "model_disagrees": FlagInfo(
        Severity.WARNING,
        "la pré-relecture automatique conteste la valeur",
        "check this field first; the model's proposal is in the detail (never applied automatically)",
    ),
    "audit_sample": FlagInfo(
        Severity.INFO, "échantillon d'audit (rejeté par le tri)", "label it: it measures what the screen misses"
    ),
}

_POWER_MENTION = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:MW|MWh|MWc|MWac|MVA|kVA)\b")
_FOURTEEN_DIGITS = re.compile(r"(?<!\d)(\d{3}\s?\d{3}\s?\d{3}\s?\d{5})(?!\d)")
_OCR_TOKEN = re.compile(r"[A-Za-zÀ-ÿ'’\-]{2,}[.,;:!?»)]*|[«(]?\d[\d.,/:\-h°%]*[.,;:)]*|[«»()\-–:;,.%°€/&]")
OCR_NOISE_THRESHOLD = 0.25
TRACKED_MISSING = ("developer", "commune", "mw_max", "filed_on")


@dataclass(frozen=True)
class ReadDocument:
    id: str
    role: DocumentRole
    text: str  # GDPR-scrubbed
    fields: dict[str, str]
    layout: str | None  # forms only: see extraction.cerfa.form_layout
    ocr_texts: dict[int, str] = field(default_factory=dict)
    ocr_engines: dict[int, str] = field(default_factory=dict)
    audit: str | None = None
    template: str = ""
    new_template: bool = False


@dataclass(frozen=True)
class QualityInput:
    documents: list[ReadDocument]
    form: Facts
    decision: Facts
    facts: Facts  # final (after merge, scrub, LLM fallback)
    classification: Classification
    checks: tuple[Check, ...]
    target: bool
    kept_by_text: bool  # the full-text screen keeps it
    unread: list[str]
    second_opinion: Opinion | None = None  # local classifier, only when proven reliable


def flag(code: str, field_name: str | None = None, detail: str = "") -> Flag:
    return Flag(code=code, severity=FLAGS[code].severity, field=field_name, detail=detail[:300])


def ocr_noise(text: str) -> float:
    """Share of tokens that look like OCR garbage (no reference needed). 0 = clean."""
    tokens = text.split()
    return 1 - sum(bool(_OCR_TOKEN.fullmatch(t)) for t in tokens) / len(tokens) if tokens else 0.0


def _reading(q: QualityInput) -> list[Flag]:
    out = [flag("unread_scan", detail=f"{len(q.unread)} document(s)")] if q.unread else []
    for doc in q.documents:
        machine = " ".join(t for p, t in doc.ocr_texts.items() if doc.ocr_engines.get(p, "") != "transcript")
        if machine and (noise := ocr_noise(machine)) > OCR_NOISE_THRESHOLD:
            out.append(flag("ocr_noisy", detail=f"{noise:.0%} de jetons douteux ({doc.id[:12]})"))
        if doc.new_template:
            out.append(flag("new_template", detail=doc.template))
        if doc.layout == "unknown":
            out.append(flag("form_layout_unknown", detail=", ".join(sorted(doc.fields)[:8])))
        elif doc.layout == "flattened" and q.target:
            out.append(flag("form_flattened"))
    return out


def _evidence(q: QualityInput) -> list[Flag]:
    texts = {d.id: d.text for d in q.documents}
    out = []
    for name, ev in q.facts.evidence.items():
        if ev.locator.startswith(("field:", "title")) or ev.document_id not in texts:
            continue
        if not contains_quote(texts[ev.document_id], ev.quote):
            out.append(flag("quote_not_found", name, ev.quote[:120]))
        if ev.locator.startswith("llm-extract:"):
            out.append(flag("llm_value", name, ev.quote[:120]))
    return out


_NAME_NOISE = frozenset({"sas", "sasu", "sarl", "sa", "societe", "la", "le", "les", "l", "de", "du", "des", "d", "et"})


def _tokens(name: str) -> set[str]:
    return {t for t in fold(name).split() if t not in _NAME_NOISE and not t.isdigit()}


def same_name(a: str, b: str) -> bool:
    """Tolerant: legal forms, articles and order numbers ignored; one name may contain the other (« X et Y »)."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta or len(ta & tb) / len(ta | tb) >= 0.6)


def _disagreements(q: QualityInput) -> list[Flag]:
    f, d = q.form, q.decision
    out = []
    masked = "masqu" in fold(f"{f.developer} {d.developer}")
    if f.developer and d.developer and not masked and not same_name(f.developer, d.developer):
        out.append(flag("disagreement", "developer", f"formulaire « {f.developer} » / décision « {d.developer} »"))
    if f.commune and d.commune and not same_name(f.commune, d.commune):
        out.append(flag("disagreement", "commune", f"formulaire « {f.commune} » / décision « {d.commune} »"))
    if f.mw_max and d.mw_max and abs(f.mw_max - d.mw_max) > 0.01 * f.mw_max:
        out.append(flag("disagreement", "power", f"formulaire {f.mw_max:g} MW / décision {d.mw_max:g} MW"))
    if f.filed_on and d.filed_on and f.filed_on != d.filed_on:
        out.append(flag("disagreement", "filed_on", f"formulaire {f.filed_on} / décision {d.filed_on}"))
    return out


def _gaps(q: QualityInput) -> list[Flag]:
    if not q.target:
        return []
    text = " ".join(d.text for d in q.documents)
    out = [flag("missing", name) for name in TRACKED_MISSING if getattr(q.facts, name) in (None, "")]
    if q.facts.mw_max is None and q.facts.mwh is None and (m := _POWER_MENTION.search(text)):
        out.append(flag("mentioned_not_extracted", "power", m[0]))
    if q.facts.siret is None:
        candidates = [re.sub(r"\s", "", m[1]) for m in _FOURTEEN_DIGITS.finditer(text)]
        if valid := [c for c in candidates if luhn_ok(c)]:
            out.append(flag("mentioned_not_extracted", "siret", valid[0]))
    return out


def _decisions(q: QualityInput) -> list[Flag]:
    out = [flag("check_alert", c.name, c.detail) for c in q.checks if c.status is CheckStatus.ALERT]
    if q.classification.quote_verified is False:
        out.append(flag("classification_unverified", detail=q.classification.quote[:120]))
    if q.classification.category.value in {"needs_review", "unclassified"}:
        out.append(flag("category_undecided"))
    opinion = q.second_opinion
    if opinion and opinion.category is not q.classification.category and opinion.probability >= CONFIDENT:
        out.append(flag("second_opinion_disagrees", detail=opinion.describe()))
    audits = [d.audit for d in q.documents if d.audit]
    if audits:
        out.append(flag("audit_sample", detail=audits[0]))
        if q.kept_by_text:
            out.append(flag("rescued_by_text", detail=audits[0]))
    return out


DETECTORS = (_reading, _evidence, _disagreements, _gaps, _decisions)
_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def detect(q: QualityInput) -> tuple[Flag, ...]:
    flags = [f for detector in DETECTORS for f in detector(q)]
    return tuple(sorted(flags, key=lambda f: (_ORDER[f.severity], f.code, f.field or "")))
