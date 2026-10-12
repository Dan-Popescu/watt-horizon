"""Document template fingerprints: which layout a document follows, so a new one is noticed, not misread.

- forms with fields: all field names, filled or not (a renamed or added field = a new template);
- other documents: role + the structural markers present (« vu le formulaire », « considerant », « article 1 »,
  issuing authority...).
Known templates live in `gold/templates.json` (`radar templates export` accepts the current ones). When that
registry exists, a document with an unknown template is flagged (`new_template`): its extraction is untested.
Metrics are also broken down per template (stratum "template").
"""

import hashlib
import json
from collections import Counter
from datetime import date
from pathlib import Path

from radar.domain.enums import DocumentRole
from radar.domain.text import fold

# Structural markers only: words that depend on the project (« avis », « enquete publique ») would split one layout.
MARKERS: tuple[str, ...] = (
    "vu le formulaire",
    "considerant",
    "article 1",
    "decide",
    "prefet",
    "dreal",
    "mission regionale",
    "cerfa",
    "14734",
)


def fingerprint(role: DocumentRole, field_names: tuple[str, ...], text: str) -> str:
    """Stable for every document of a layout: all field names (filled or not), else structural markers."""
    if field_names:
        names = "|".join(sorted(field_names))
        return f"{role.value}:fields-{hashlib.sha1(names.encode()).hexdigest()[:8]}"
    head = fold(text[:6000])
    present = "".join("1" if m in head else "0" for m in MARKERS)
    return f"{role.value}:text-{hashlib.sha1(present.encode()).hexdigest()[:8]}"


class Registry:
    def __init__(self, known: dict[str, dict[str, object]] | None = None) -> None:
        self.known = known or {}

    def __contains__(self, template: str) -> bool:
        return template in self.known

    @classmethod
    def load(cls, path: Path) -> "Registry | None":
        """None when no registry exists yet: then nothing is flagged as new."""
        return cls(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None

    def accept(self, counts: Counter[str], today: date) -> None:
        for template, n in counts.items():
            entry = self.known.setdefault(template, {"first_seen": today.isoformat(), "label": ""})
            entry["documents"] = n

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.known, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
