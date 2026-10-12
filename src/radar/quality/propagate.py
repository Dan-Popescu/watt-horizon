"""Make one correction count many times.

- Suggestions: a corrected machine value (field, wrong -> right) that the machine also produced on other,
  unreviewed filings is proposed for those filings in one click (the reviewer confirms; nothing is silent).
- Aliases: when a correction only changes how a name is written (same tokens once legal forms and articles are
  ignored: « la SAS poste électrique du chemin de Vailly » -> « POSTE ELECTRIQUE DU CHEMIN DE VAILLY 2 »), the
  variant is learned. `gold/aliases.json` (versioned, `radar aliases export`) is applied at every build, so
  every future occurrence is written the verified way automatically.
"""

import dataclasses
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from radar.domain.fields import Value, machine_values
from radar.domain.models import Correction, Filing
from radar.domain.text import fold
from radar.evaluation.fields import compare
from radar.pipeline.corrections import VALIDATED, latest
from radar.quality.flags import same_name

ALIAS_FIELDS = ("developer", "substation", "commune")


@dataclass
class Suggestion:
    field: str
    wrong: Value
    right: Value
    learned_from: str  # filing number of the correction
    cause: str | None
    keys: list[str] = dataclasses.field(default_factory=list)  # unreviewed filings with the same wrong machine value
    numbers: list[str] = dataclasses.field(default_factory=list)


def _errors(filings: list[Filing], corrections: Iterable[Correction]) -> list[tuple[Filing, Correction, Value]]:
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    out = []
    for f in filings:
        machine = machine_values(f)
        for name, c in latest(by_key.get(f.key, [])).items():
            if name != VALIDATED and name in machine and compare(c.value, machine[name]) == "wrong":
                out.append((f, c, machine[name]))
    return out


def suggestions(filings: Iterable[Filing], corrections: Iterable[Correction]) -> list[Suggestion]:
    items, all_corrections = list(filings), list(corrections)
    reviewed = {c.filing_key for c in all_corrections}
    out: dict[tuple[str, str, str], Suggestion] = {}
    for f, c, wrong in _errors(items, all_corrections):
        key = (c.field, fold(str(wrong)), fold(str(c.value)))
        s = out.setdefault(key, Suggestion(c.field, wrong, c.value, f.number, c.cause))
        for other in items:
            if other.key in reviewed or other.key in s.keys:
                continue
            if compare(wrong, machine_values(other).get(c.field)) == "correct":
                s.keys.append(other.key)
                s.numbers.append(other.number)
    return [s for s in out.values() if s.keys]


def learn_aliases(filings: Iterable[Filing], corrections: Iterable[Correction]) -> dict[str, dict[str, str]]:
    """field -> {folded machine variant: verified spelling}, from corrections that only rewrite a name."""
    aliases: dict[str, dict[str, str]] = {}
    for _, c, wrong in _errors(list(filings), corrections):
        if (
            c.field in ALIAS_FIELDS
            and isinstance(wrong, str)
            and isinstance(c.value, str)
            and same_name(wrong, c.value)
        ):
            aliases.setdefault(c.field, {})[fold(wrong)] = c.value
    return aliases


@dataclass
class Aliases:
    table: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)

    def apply(self, field_name: str, value: str | None) -> str | None:
        if not value:
            return value
        return self.table.get(field_name, {}).get(fold(value), value)

    @classmethod
    def load(cls, path: Path) -> "Aliases":
        return cls(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.table, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")


def merge(existing: Aliases, learned: dict[str, dict[str, str]]) -> Aliases:
    table = {k: dict(v) for k, v in existing.table.items()}
    for name, variants in learned.items():
        table.setdefault(name, {}).update(variants)
    return Aliases(table)


def as_dict(s: Suggestion) -> dict[str, object]:
    return asdict(s)
