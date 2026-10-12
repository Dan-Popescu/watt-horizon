"""See the effect of a code change on the WHOLE corpus, not only on references.

- `diff_machine(old, new)`: machine values that changed between two builds of a filing.
- `shadow(...)`: rebuild every filing in memory with the current code (nothing written), compare with the stored
  machine output, and judge each change against human-verified values: fixes / breaks / unknown (no reference).
  `radar shadow --strict` fails when a change breaks a verified value: run it before adopting an extractor change.
`process_filings` also records every machine change (`machine_changes` table) so unreviewed ones reach the queue.
"""

from collections import Counter
from dataclasses import dataclass, field

from radar.domain.fields import Value
from radar.domain.models import Correction, Filing
from radar.evaluation.fields import compare, same
from radar.pipeline.corrections import latest
from radar.pipeline.filings import Services, build_filing
from radar.quality.arbiter import verified_values
from radar.store.db import Database


def diff_machine(old: Filing | None, new: Filing) -> list[tuple[str, Value, Value]]:
    if old is None or old.machine is None or new.machine is None:
        return []
    before, after = old.machine.values, new.machine.values
    changes = []
    for name in sorted(set(before) | set(after)):
        a, b = before.get(name), after.get(name)
        if (a in (None, "")) and (b in (None, "")):
            continue
        if a in (None, "") or b in (None, "") or not same(a, b):
            changes.append((name, a, b))
    return changes


@dataclass
class ShadowChange:
    number: str
    field: str
    before: Value
    after: Value
    verdict: str  # fixes | breaks | changes-wrong-to-wrong | unknown


@dataclass
class ShadowReport:
    filings: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    changes: list[ShadowChange] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, object]:
        return {
            "filings": self.filings,
            "changed_values": len(self.changes),
            "changed_filings": len({c.number for c in self.changes}),
            "by_field": dict(Counter(c.field for c in self.changes)),
            "by_verdict": dict(Counter(c.verdict for c in self.changes)),
        }


def _verdict(expected: Value, before: Value, after: Value) -> str:
    ok_before = compare(expected, before) in {"correct", "absent"}
    ok_after = compare(expected, after) in {"correct", "absent"}
    if ok_after and not ok_before:
        return "fixes"
    if ok_before and not ok_after:
        return "breaks"
    return "changes-wrong-to-wrong" if not ok_after else "unchanged-verdict"


def shadow(db: Database, services: Services, only: set[str] | None = None) -> ShadowReport:
    report = ShadowReport()
    corrections: dict[str, list[Correction]] = {}
    for c in db.corrections():
        corrections.setdefault(c.filing_key, []).append(c)
    for key in db.filing_keys_with_documents():
        if only is not None and key not in only:
            continue
        old = db.filing(key)
        try:
            new = build_filing(key, db.documents(key), services, corrections.get(key, []))
        except Exception as exc:  # reported, never raised: a shadow run must survive bad documents
            report.failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        report.filings += 1
        decisions = latest(corrections.get(key, []))
        expected = verified_values(old, decisions) if old is not None and decisions else {}
        for name, before, after in diff_machine(old, new):
            verdict = _verdict(expected[name], before, after) if name in expected else "unknown"
            report.changes.append(ShadowChange(new.number, name, before, after, verdict))
    return report
