"""Regression cases: every human-verified filing becomes a replayable test of the rule extraction.

A case stores what the rules read (GDPR-scrubbed text and form fields of each document, person fields removed)
and the values a human verified (validated as-is or corrected). `replay` runs the same `extract_facts` on it.
Per field status: "fixed" (the rules give the verified value: must keep passing, a failure is a REGRESSION) or
"open" (a known error the rules still make: fix it, then `radar cases check` marks it fixed).
Files: `gold/cases/<number>.json`, versioned. Tests: `tests/test_gold_cases.py`.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from radar.domain.enums import DocumentRole
from radar.domain.fields import Value
from radar.domain.models import DocumentRecord, Filing
from radar.domain.privacy import PersonScrubber, is_person_field
from radar.evaluation.fields import compare
from radar.evaluation.stats import partition_of
from radar.pipeline.filings import extract_facts
from radar.quality.rulebook import LearnedRule
from radar.reading.pdf import PdfContent

REPLAYABLE: dict[str, str] = {  # field -> Facts attribute (category needs a classifier: not replayed here)
    "developer": "developer",
    "siret": "siret",
    "commune": "commune",
    "power_mw": "mw_max",
    "power_mw_min": "mw_min",
    "energy_mwh": "mwh",
    "substation": "substation",
    "filed_on": "filed_on",
    "review_decision": "review_decision",
}


@dataclass
class CaseDocument:
    id: str
    role: str
    title: str
    text: str
    fields: dict[str, str]


@dataclass
class Case:
    number: str
    expected: dict[str, Value]  # verified values, replayable fields only
    documents: list[CaseDocument]
    status: dict[str, str] = field(default_factory=dict)  # field -> open | fixed
    created_on: str = field(default_factory=lambda: date.today().isoformat())


def make_case(
    filing: Filing, documents: list[DocumentRecord], contents: list[PdfContent], verified: dict[str, Value]
) -> Case:
    if partition_of(filing) != "dev":
        raise ValueError("Only development filings may become regression cases")
    privacy = PersonScrubber.for_filing([x.fields for x in contents], [filing.developer, filing.legal_name])
    docs = [
        CaseDocument(
            id=d.id,
            role=d.role.value,
            title=privacy.scrub(d.title) or "",
            text=privacy.scrub(x.text) or "",
            fields={k: privacy.scrub(v) or "" for k, v in x.fields.items() if not is_person_field(k)},
        )
        for d, x in zip(documents, contents, strict=True)
    ]
    case = Case(filing.number, {k: v for k, v in verified.items() if k in REPLAYABLE}, docs)
    case.status = {name: "fixed" if ok else "open" for name, ok in check(case).items()}
    return case


def replay(case: Case, rules: Sequence[LearnedRule] = ()) -> dict[str, Value]:
    contents = [PdfContent(pages=1, fields=d.fields, page_texts=(d.text,), scanned_pages=()) for d in case.documents]
    facts = extract_facts([(d.id, DocumentRole(d.role), d.title) for d in case.documents], contents, rules=rules).facts
    out: dict[str, Value] = {}
    for name, attr in REPLAYABLE.items():
        value = getattr(facts, attr)
        out[name] = value.isoformat() if isinstance(value, date) else getattr(value, "value", value)
    return out


def check(case: Case, rules: Sequence[LearnedRule] = ()) -> dict[str, bool]:
    got = replay(case, rules)
    return {name: compare(expected, got[name]) in {"correct", "absent"} for name, expected in case.expected.items()}


def save(case: Case, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.number}.json"
    path.write_text(json.dumps(asdict(case), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


def load(path: Path) -> Case:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    raw["documents"] = [CaseDocument(**d) for d in raw["documents"]]
    return Case(**raw)


def load_all(directory: Path) -> list[Case]:
    return [load(p) for p in sorted(directory.glob("*.json"))] if directory.exists() else []


def refresh(directory: Path, rules: Sequence[LearnedRule] = ()) -> dict[str, list[str]]:
    """Replay every case; open fields that now pass become fixed; fixed fields that fail are regressions."""
    result: dict[str, list[str]] = {"open": [], "fixed": [], "newly_fixed": [], "regressed": []}
    for case in load_all(directory):
        changed = False
        for name, ok in check(case, rules).items():
            label = f"{case.number}:{name}"
            if ok and case.status.get(name) != "fixed":
                case.status[name], changed = "fixed", True
                result["newly_fixed"].append(label)
            elif not ok and case.status.get(name) == "fixed":
                result["regressed"].append(label)
            result[case.status.get(name, "open")].append(label)
        if changed:
            save(case, directory)
    return result
