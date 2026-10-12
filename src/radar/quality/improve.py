"""Improvement tickets: one agent-ready Markdown task per field with failing regression cases.

Each ticket lists the failing cases (verified value, current rule output, where the value sits in the text),
validated rule candidates (quality/suggest.py), the code to change and the commands that prove the fix.
Written to `<data>/improve/<field>.md` by `radar improve`; English, for coding agents.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from radar.domain.fields import Value
from radar.quality.cases import Case, check, replay
from radar.quality.locate import locate
from radar.quality.suggest import Suggestion, suggest

WHERE: dict[str, list[str]] = {
    "developer": ["src/radar/extraction/decision.py (_DEVELOPER)", "src/radar/extraction/cerfa.py (FIELD_ALIASES)"],
    "siret": ["src/radar/domain/ids.py (find_siret)", "src/radar/extraction/cerfa.py (FIELD_ALIASES)"],
    "commune": [
        "src/radar/extraction/decision.py (_COMMUNE)",
        "src/radar/extraction/cerfa.py (_commune_from_locality)",
    ],
    "power_mw": ["src/radar/domain/power.py (read_power)", "src/radar/extraction/cerfa.py (_from_fields)"],
    "power_mw_min": ["src/radar/domain/power.py (read_power, ranges)"],
    "energy_mwh": ["src/radar/domain/power.py (read_power, _MWH)"],
    "substation": ["src/radar/extraction/substation.py (_POSTE, _NAME)"],
    "filed_on": ["src/radar/extraction/decision.py (_DATE)", "src/radar/extraction/cerfa.py (_complete_from_text)"],
    "review_decision": ["src/radar/extraction/decision.py (_ARTICLE_1)"],
}


@dataclass
class Failure:
    number: str
    expected: Value
    got: Value
    context: str  # "...left [VALUE] right..." or why it was not found


@dataclass
class Ticket:
    field: str
    failures: list[Failure] = dataclasses.field(default_factory=list)
    suggestions: list[Suggestion] = dataclasses.field(default_factory=list)


def tickets(cases: list[Case]) -> list[Ticket]:
    out: dict[str, Ticket] = {}
    for case in cases:
        got = replay(case)
        for name, ok in check(case).items():
            if ok:
                continue
            texts = {d.id: d.text for d in case.documents}
            spans = locate(name, case.expected[name], texts, limit=1)
            if spans:
                s = spans[0]
                context = f"…{' '.join(s.left.split())[-70:]} [{s.text}] {' '.join(s.right.split())[:50]}…"
            elif case.expected[name] is None:
                context = "reference says ABSENT: the rules invent a value"
            else:
                context = "value not found verbatim in the stored text (OCR? other wording? check the PDF)"
            out.setdefault(name, Ticket(name)).failures.append(
                Failure(case.number, case.expected[name], got[name], context)
            )
    for ticket in out.values():
        ticket.suggestions = suggest(cases, ticket.field)
    return sorted(out.values(), key=lambda t: -len(t.failures))


def render(ticket: Ticket) -> str:
    lines = [
        f"# Improve extraction of `{ticket.field}` — {len(ticket.failures)} failing case(s)",
        "",
        "Goal: make these regression cases pass without breaking any other case.",
        "",
        "## Failing cases (verified value vs current rules)",
        "",
        "| case | verified | rules give | where the verified value is |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {f.number} | `{f.expected}` | `{f.got}` | {f.context.replace('|', '/')} |" for f in ticket.failures]
    lines += ["", "## Rule candidates (validated on every verified case)", ""]
    if ticket.suggestions:
        lines += ["Regexes apply to `quality.locate.fold_keep(text)`; group 1 is the value.", ""]
        lines += ["| regex | fixes | correct | wrong | spurious | precision |", "| --- | --- | --- | --- | --- | --- |"]
        lines += [
            f"| `{s.regex}` | {', '.join(s.fixes)} | {s.correct} | {s.wrong} | {s.spurious} | {s.precision:.0%} |"
            for s in ticket.suggestions
        ]
    else:
        lines.append("No safe candidate found: read the contexts above (wording, OCR, layout).")
    lines += [
        "",
        "## Where to change",
        "",
        *[f"- `{w}`" for w in WHERE.get(ticket.field, ["src/radar/extraction/"])],
        "",
        "## Done when",
        "",
        "```bash",
        "uv run pytest tests/test_gold_cases.py tests/test_extraction.py",
        "uv run radar cases check --strict      # open cases above become fixed, no regression",
        "uv run radar run && uv run radar eval --strict",
        "```",
        "",
        "Bump `EXTRACTION_VERSION` (src/radar/extraction/__init__.py). Add a unit test with the verbatim context.",
    ]
    return "\n".join(lines) + "\n"


def write(all_tickets: list[Ticket], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.md"):
        old.unlink()  # tickets are regenerated from the current state each time
    paths = []
    for ticket in all_tickets:
        path = directory / f"{ticket.field}.md"
        path.write_text(render(ticket), encoding="utf-8", newline="\n")
        paths.append(path)
    return paths
