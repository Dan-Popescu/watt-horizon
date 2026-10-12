"""Field-level reference values ("field gold"): what a human read in the documents, per filing.

File `data/gold_fields.jsonl`, one filing per line:
  {"number": "012667", "expected": {"power_mw": 47, "substation": "Margot", "siret": null, ...},
   "annotator": "Dan", "verified": true, "note": "..."}
`null` means "absent from the documents": extracting something there is an error (spurious).
A field missing from `expected` is not evaluated. Drafts (`verified: false`) are reported separately.
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.domain.models import Filing

FIELDS: tuple[str, ...] = (
    "developer",
    "siret",
    "commune",
    "department",
    "power_mw",
    "power_mw_min",
    "energy_mwh",
    "substation",
    "filed_on",
    "review_decision",
)
Value = str | float | None


@dataclass(frozen=True)
class FieldGold:
    number: str
    expected: dict[str, Value]
    annotator: str = ""
    verified: bool = False
    note: str = ""


def actual_fields(filing: Filing) -> dict[str, Value]:
    """The values `FIELDS` names, read from a stored filing (only declared powers count)."""
    known = filing.power.status.value == "known"
    return {
        "developer": filing.developer,
        "siret": filing.siret,
        "commune": filing.location.commune,
        "department": filing.location.department,
        "power_mw": filing.power.mw if known else None,
        "power_mw_min": filing.power.mw_min if known else None,
        "energy_mwh": filing.power.mwh,
        "substation": filing.substation,
        "filed_on": filing.filed_on.isoformat() if filing.filed_on else None,
        "review_decision": filing.review_decision.value,
    }


def load_field_gold(path: Path) -> dict[str, FieldGold]:
    if not path.exists():
        return {}
    items: dict[str, FieldGold] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            unknown = set(row["expected"]) - set(FIELDS)
            if unknown:
                raise ValueError(f"{path}: unknown fields {sorted(unknown)} for {row['number']}")
            items[row["number"]] = FieldGold(**row)
    return items


def write_field_gold(path: Path, items: Iterable[FieldGold]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    lines = [json.dumps(asdict(g), ensure_ascii=False) for g in sorted(items, key=lambda g: g.number)]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def draft(filings: Iterable[Filing], existing: dict[str, FieldGold], version: str) -> list[FieldGold]:
    """Unverified drafts from current extractions, for filings without a reference yet. A human must confirm
    each value (review page) before it counts as verified: a draft only detects regressions."""
    return [
        FieldGold(number=f.number, expected=actual_fields(f), annotator=f"draft:extraction-{version}")
        for f in filings
        if f.number not in existing
    ]
