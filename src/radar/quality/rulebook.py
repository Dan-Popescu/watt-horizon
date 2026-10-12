"""Adopted rules: regex rules added WITHOUT code changes (`radar rules adopt`), versioned in `gold/rules.json`.

Each active rule runs on every document (on `fold_keep` text); its value becomes a candidate (source
"rule:<id>") for the arbiter, which learns its reliability from reviews like any other source. A value is kept
only if found verbatim in the document (`locate`): rules obey the evidence invariant. A rule whose learned
reliability falls below DISABLE_BELOW after MIN_SEEN reviewed values is ignored automatically (`effective`).
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from radar.domain.fields import Value
from radar.domain.models import Candidate, Evidence
from radar.quality.locate import fold_keep, locate
from radar.quality.values import parse

RULE_FIELDS = ("developer", "siret", "commune", "power_mw", "energy_mwh", "substation", "filed_on")
MIN_SEEN = 10
DISABLE_BELOW = 0.6


@dataclass
class LearnedRule:
    id: str
    field: str
    regex: str  # on fold_keep() text; group 1 = the value
    status: str = "active"  # active | disabled (by hand)
    created_on: str = ""
    origin: str = ""  # e.g. "suggestion: fixes 2025-8646, 2025-9147"
    note: str = ""


def rule_id(field_name: str, regex: str) -> str:
    return hashlib.sha1(f"{field_name}|{regex}".encode()).hexdigest()[:8]


class Rulebook:
    def __init__(self, rules: list[LearnedRule] | None = None) -> None:
        self.rules = rules or []

    @classmethod
    def load(cls, path: Path) -> "Rulebook":
        if not path.exists():
            return cls()
        return cls([LearnedRule(**r) for r in json.loads(path.read_text(encoding="utf-8"))])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([asdict(r) for r in self.rules], ensure_ascii=False, indent=1) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")

    def adopt(self, field_name: str, regex: str, origin: str = "", note: str = "") -> LearnedRule:
        if field_name not in RULE_FIELDS:
            raise ValueError(f"field must be one of {', '.join(RULE_FIELDS)}")
        compiled = re.compile(regex)
        if compiled.groups < 1:
            raise ValueError("the regex needs a capture group: group 1 is the value")
        rid = rule_id(field_name, regex)
        if existing := next((r for r in self.rules if r.id == rid), None):
            existing.status = "active"
            return existing
        rule = LearnedRule(rid, field_name, regex, created_on=date.today().isoformat(), origin=origin, note=note)
        self.rules.append(rule)
        return rule

    def disable(self, rid: str) -> None:
        rule = next((r for r in self.rules if r.id == rid), None)
        if rule is None:
            raise ValueError(f"unknown rule {rid}")
        rule.status = "disabled"

    def effective(self, reliability: dict[str, tuple[int, int]] | None = None) -> tuple[LearnedRule, ...]:
        """Active rules, minus those that reviews proved unreliable. `reliability`: rule id -> (correct, seen)."""
        out = []
        for r in self.rules:
            correct, seen = (reliability or {}).get(r.id, (0, 0))
            if r.status == "active" and not (seen >= MIN_SEEN and correct / seen < DISABLE_BELOW):
                out.append(r)
        return tuple(out)


@dataclass(frozen=True)
class RuleHit:
    candidate: Candidate
    value: Value
    evidence: Evidence


def apply_rules(rules: Iterable[LearnedRule], documents: dict[str, str]) -> dict[str, list[RuleHit]]:
    """field -> hits (first match per rule, verified verbatim in its document)."""
    folded = {doc_id: fold_keep(text) for doc_id, text in documents.items()}
    hits: dict[str, list[RuleHit]] = {}
    for rule in rules:
        compiled = re.compile(rule.regex)
        for doc_id, text in folded.items():
            m = compiled.search(text)
            if not m:
                continue
            value = parse(rule.field, m[1])
            spans = locate(rule.field, value, {doc_id: documents[doc_id]}, limit=1) if value is not None else []
            if spans:  # the evidence invariant: no verbatim passage, no value
                s = spans[0]
                if isinstance(value, str) and rule.field in {"developer", "commune", "substation"}:
                    value = " ".join(s.text.split())  # names as written in the document, not folded
                quote = " ".join(f"{s.left[-60:]}{s.text}{s.right[:60]}".split())
                evidence = Evidence(document_id=doc_id, locator=f"rule:{rule.id}", quote=quote)
                hits.setdefault(rule.field, []).append(
                    RuleHit(Candidate(source=f"rule:{rule.id}", value=value), value, evidence)
                )
            break
    return hits
