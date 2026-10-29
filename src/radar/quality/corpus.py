"""A small corpus of REAL public PDFs, redacted, versioned in `tests/corpus/`: the whole chain (PDF reading ->
filings -> projects) is replayed at every test run and compared with a reviewed snapshot (`expected.json`).
It catches what text-based cases cannot: a PyMuPDF/Tesseract upgrade or a reading change that alters text.

GDPR: documents are redacted before they enter the repository - person form fields emptied; person names (from
form fields), civility names, e-mails, phones and signature blocks removed from the text layer; every image
removed (signatures, stamps); digital signature fields removed (their certificate names the signer). Scanned
documents are excluded (names in images cannot be redacted reliably).
`audit` re-reads the redacted PDF and must find nothing. Source: DREAL Hauts-de-France, Licence Ouverte.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from radar.classification.gold import GoldClassifier, GoldLabel
from radar.domain.enums import Category, DocumentRole, SourceKind
from radar.domain.fields import field_values
from radar.domain.privacy import PersonScrubber, is_person_field, person_names
from radar.pipeline.filings import Services
from radar.pipeline.ingest import ingest_pdf
from radar.pipeline.run import link_and_build, process_filings
from radar.reading.ocr import NullOcr
from radar.reading.pdf import read_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import CommuneInfo, StaticLookups

SIGNATURE = re.compile(
    r"(?i)^\s*(?:pour le pr[ée]fet|le pr[ée]fet|la pr[ée]f[èe]te|pour le directeur|le directeur|la directrice|"
    r"par d[ée]l[ée]gation|par subd[ée]l[ée]gation|le chef|la cheffe|l.adjoint|sign[ée])"
)
CIVILITY_OR_CONTACT = re.compile(
    r"(?:\b(?:M\.|Mme|Mlle|Monsieur|Madame)\s+(?:[A-ZÉÈ][\w'’-]+\s?){1,3})|[\w.+-]+@[\w-]+\.[\w.-]+|(?:\+33\s?|0)[1-9](?:[ .-]?\d{2}){4}"
)
IMAGE_REMOVE: int = getattr(pymupdf, "PDF_REDACT_IMAGE_REMOVE", 1)  # not in PyMuPDF's type stubs
IMAGE_KEEP: int = getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0)
TEXT_KEEP: int = getattr(pymupdf, "PDF_REDACT_TEXT_NONE", 1)
SIGNATURE_FIELD: int = getattr(pymupdf, "PDF_WIDGET_TYPE_SIGNATURE", 6)
SIGNATURE_LINES = 3  # the signature title line and the lines after it (name, function)


def redact_page_text(page: Any, names: set[str]) -> None:
    """Black out person data in the page's text layer (images untouched)."""
    lines = page.get_text("text").splitlines()
    targets: set[str] = set()
    for i, line in enumerate(lines):
        if SIGNATURE.search(line):
            targets |= {x.strip() for x in lines[i : i + SIGNATURE_LINES] if len(x.strip()) > 2}
        targets |= {m[0].strip() for m in CIVILITY_OR_CONTACT.finditer(line)}
    targets |= names
    for target in targets:
        for rect in page.search_for(target):
            page.add_redact_annot(rect, fill=(0, 0, 0))
    page.apply_redactions(images=IMAGE_KEEP)


def _redact_page(page: Any, names: set[str]) -> None:
    redact_page_text(page, names)
    for xref, *_ in page.get_images(full=True):  # signatures, stamps, logos: none is needed for extraction
        for rect in page.get_image_rects(xref):
            page.add_redact_annot(rect)
    page.apply_redactions(images=IMAGE_REMOVE, text=TEXT_KEEP)


def redact(data: bytes) -> bytes:
    names = person_names(read_pdf(data).fields)
    with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        for page in doc:
            # Person fields are deleted, not emptied: a field shared by several widgets keeps its value otherwise.
            # Digital signatures embed the signer's certificate (a name): their fields go too.
            doomed = [
                w
                for w in page.widgets() or []
                if (w.field_name and is_person_field(w.field_name)) or w.field_type == SIGNATURE_FIELD
            ]
            for widget in doomed:
                page.delete_widget(widget)
            _redact_page(page, names)
        doc.set_metadata({})
        return bytes(doc.tobytes(garbage=4, deflate=True))


def audit(original: bytes, redacted: bytes) -> list[str]:
    """Person data still readable in the redacted PDF (expected: nothing)."""
    names = person_names(read_pdf(original).fields)
    content = read_pdf(redacted)
    text = content.text + " " + " ".join(content.fields.values())
    leaks = PersonScrubber(frozenset(names)).leaks(text)
    with pymupdf.open(stream=redacted, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        if any(page.get_images() for page in doc):
            leaks.append("images remain")
        objects = (doc.xref_object(x, compressed=True) for x in range(1, doc.xref_length()))
        if any("/ByteRange" in o or "/Type/Sig" in o for o in objects):
            leaks.append("digital signature remains (certificate holds a name)")
    return leaks


@dataclass
class CorpusDocument:
    filename: str
    role: str
    title: str
    url: str


@dataclass
class CorpusFiling:
    number: str
    category: str  # label used as gold for the replay (no model needed)
    documents: list[CorpusDocument]


def load_manifest(directory: Path) -> tuple[list[CorpusFiling], list[CommuneInfo]]:
    raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    filings = [
        CorpusFiling(f["number"], f["category"], [CorpusDocument(**d) for d in f["documents"]]) for f in raw["filings"]
    ]
    return filings, [CommuneInfo(**c) for c in raw["communes"]]


def replay(directory: Path, workdir: Path) -> dict[str, Any]:
    """Run the whole chain on the corpus PDFs in a scratch database; return the comparable snapshot."""
    filings, communes = load_manifest(directory)
    db, blobs = Database(workdir / "corpus.sqlite"), BlobStore(workdir / "blobs")
    with db, db.transaction():
        for f in filings:
            for d in f.documents:
                ingest_pdf(
                    db, blobs, data=(directory / "pdfs" / d.filename).read_bytes(), source=SourceKind.DREAL_CASE_BY_CASE,
                    region="hauts-de-france", number=f.number, url=d.url, title=d.title, filename=d.filename,
                    role=DocumentRole(d.role),
                )  # fmt: skip
    with Database(workdir / "corpus.sqlite") as db:
        gold = GoldClassifier(
            {f.number: GoldLabel(f.number, Category(f.category)) for f in filings},
            known_keys=db.filing_keys_with_documents(),
        )
        services = Services(blobs=blobs, ocr=NullOcr(), classifier=gold, lookups=StaticLookups(communes=communes))
        process_filings(db, services)
        projects = link_and_build(db)["projects"]
        return {
            "projects": projects,
            "filings": {
                f.number: {
                    "category": f.classification.category.value,
                    "values": field_values(f),
                    "flags": sorted({g.code for g in f.flags}),
                    "read": f.strata.get("read"),
                }
                for f in db.filings()
            },
        }


def diff(expected: dict[str, Any], got: dict[str, Any]) -> list[str]:
    out = [] if expected["projects"] == got["projects"] else [f"projects: {expected['projects']} -> {got['projects']}"]
    for number in sorted(set(expected["filings"]) | set(got["filings"])):
        a, b = expected["filings"].get(number, {}), got["filings"].get(number, {})
        for key in sorted(set(a) | set(b)):
            if key == "values":
                for name in sorted(set(a.get("values", {})) | set(b.get("values", {}))):
                    if a.get("values", {}).get(name) != b.get("values", {}).get(name):
                        out.append(f"{number} {name}: {a['values'].get(name)!r} -> {b['values'].get(name)!r}")
            elif a.get(key) != b.get(key):
                out.append(f"{number} {key}: {a.get(key)!r} -> {b.get(key)!r}")
    return out
