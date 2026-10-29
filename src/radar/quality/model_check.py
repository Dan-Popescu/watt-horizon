"""Model pre-review: before a human, a (cheap) model re-reads each uncertain value against the passage it came
from and says agree / disagree / unsure. It saves review time without ever becoming a reference:

- it is never a correction and never gold: evaluation and learning only use human reviews;
- a « disagree » adds the `model_disagrees` flag (the reviewer looks there first, with the model's proposal);
- an « agree » removes the field from the uncertain ones in the review queue ONLY once the model is proven reliable
  on fields a human reviewed (`reliability`: Wilson lower bound of its « agree » being right >= RELIABLE over at
  least MIN_SEEN fields), the same rule as auto-accepted fields. Until then it changes nothing but the flag.
"""

import dataclasses
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from radar.domain.fields import Value
from radar.domain.models import Correction, Filing
from radar.evaluation.fields import compare
from radar.evaluation.stats import partition_of, wilson
from radar.pipeline.corrections import approved_values, latest
from radar.store.cache import KeyValueCache, cache_key

if TYPE_CHECKING:
    import anthropic

PROMPT_VERSION = "2026-10-13"
CHECKED_FIELDS = ("developer", "siret", "commune", "power_mw", "energy_mwh", "substation", "filed_on")
EVIDENCE_OF = {"power_mw": "power", "energy_mwh": "power"}
UNCERTAIN = 0.9  # calibrated confidence below which a value is worth a second look
CONTEXT = 500  # characters around the quote
MIN_SEEN = 30
RELIABLE = 0.9
SYSTEM_PROMPT = (
    "Tu vérifies des valeurs extraites de documents administratifs français. Pour chaque champ, dis si le passage "
    "fourni confirme la valeur (agree), la contredit (disagree, avec la bonne valeur si elle y figure) ou ne permet "
    "pas de trancher (unsure). Ne te fie qu'au passage, n'invente rien."
)
VERDICT = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["agree", "disagree", "unsure"]},
        "value": {"type": ["string", "null"]},
    },
    "required": ["verdict", "value"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CheckItem:
    field: str
    value: str
    passage: str


@dataclass(frozen=True)
class CheckInput:
    filing_key: str
    title: str
    items: tuple[CheckItem, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "CheckInput":
        return cls(raw["filing_key"], raw["title"], tuple(CheckItem(**i) for i in raw["items"]))


def passage(text: str, quote: str, width: int = CONTEXT) -> str:
    """The quote with `width` characters on each side, located whitespace-insensitively."""
    words = [re.escape(w) for w in quote.split()[:12]]
    m = re.search(r"\s+".join(words), text) if words else None
    if m is None:
        return quote
    return text[max(0, m.start() - width) : m.end() + width]


def build_input(filing: Filing, texts_by_doc: dict[str, str], values: dict[str, Value]) -> CheckInput | None:
    """Uncertain values that have a quote (a missing value is the extraction fallback's job, not this one's)."""
    items = []
    for name in CHECKED_FIELDS:
        value, ev = values.get(name), filing.evidence.get(EVIDENCE_OF.get(name, name))
        confidence = filing.field_confidence.get(name, 0.0)
        if value in (None, "") or ev is None or confidence >= UNCERTAIN or name in filing.auto_accepted:
            continue
        items.append(CheckItem(name, str(value), passage(texts_by_doc.get(ev.document_id, ""), ev.quote)))
    return CheckInput(filing.key, filing.title, tuple(items)) if items else None


Verdicts = dict[str, dict[str, str | None]]  # field -> {"verdict": ..., "value": ...}


class LlmVerifier:
    NAMESPACE = "model-check"

    def __init__(self, model: str, client: "anthropic.Anthropic | None" = None) -> None:
        self.model, self._client = model, client
        self.name = f"check:{model}@{PROMPT_VERSION}"

    @property
    def client(self) -> "anthropic.Anthropic":
        if self._client is None:
            import anthropic  # lazy: optional extra

            self._client = anthropic.Anthropic()
        return self._client

    def request(self, item: CheckInput) -> dict[str, Any]:
        fields = {i.field for i in item.items}
        schema = {
            "type": "object",
            "properties": dict.fromkeys(sorted(fields), VERDICT),
            "required": sorted(fields),
            "additionalProperties": False,
        }
        body = "\n\n".join(f"### {i.field}\nValeur extraite : {i.value}\nPassage :\n{i.passage}" for i in item.items)
        return {
            "model": self.model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": f"Dossier : {item.title}\n\n{body}"}],
        }

    def key(self, item: CheckInput) -> str:
        return cache_key(self.name, json.dumps(self.request(item), sort_keys=True, ensure_ascii=False))

    def parse(self, message: Any, item: CheckInput) -> Verdicts:
        if getattr(message, "stop_reason", None) == "refusal":
            return {}
        raw = next((block.text for block in message.content if block.type == "text"), None)
        if not raw:
            return {}
        out = json.loads(raw)
        return {i.field: out[i.field] for i in item.items if isinstance(out.get(i.field), dict)}

    def check(self, item: CheckInput) -> Verdicts:
        response = self.client.beta.messages.create(
            **self.request(item), betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
        return self.parse(response, item)


class CachedVerifier:
    NAMESPACE = LlmVerifier.NAMESPACE

    def __init__(self, inner: LlmVerifier, cache: KeyValueCache) -> None:
        self.inner, self.cache, self.name = inner, cache, inner.name

    def key(self, item: CheckInput) -> str:
        return self.inner.key(item)

    def check(self, item: CheckInput) -> Verdicts:
        key = self.inner.key(item)
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            return dict(hit)
        result = self.inner.check(item)
        self.cache.put(self.NAMESPACE, key, result)
        return result


@dataclass
class Reliability:
    agree_seen: int = 0  # « agree » on a field a human then reviewed
    agree_right: int = 0  # ... and the human kept the value
    disagree_seen: int = 0
    disagree_right: int = 0  # « disagree » and the human did change the value
    by_field: dict[str, tuple[int, int, int, int]] = dataclasses.field(default_factory=dict)

    @property
    def proven(self) -> bool:
        if self.by_field:
            return all(self.proven_for(name) for name in self.by_field)
        interval = wilson(self.agree_right, self.agree_seen)
        return self.agree_seen >= MIN_SEEN and interval is not None and interval[0] >= RELIABLE

    def proven_for(self, name: str) -> bool:
        seen, right, _, _ = self.by_field.get(name, (0, 0, 0, 0))
        interval = wilson(right, seen)
        return seen >= MIN_SEEN and interval is not None and interval[0] >= RELIABLE

    def as_dict(self) -> dict[str, Any]:
        interval = wilson(self.agree_right, self.agree_seen)
        return asdict(self) | {
            "agree_interval": [round(x, 3) for x in interval] if interval else None,
            "proven": self.proven,
            "rule": f">= {MIN_SEEN} reviewed « agree » and lower bound >= {RELIABLE}",
        }


def reliability(filings: list[Filing], corrections: list[Correction]) -> Reliability:
    """The model measured against the human: only fields a human validated or corrected count."""
    by_key: dict[str, list[Correction]] = {}
    for c in corrections:
        by_key.setdefault(c.filing_key, []).append(c)
    out = Reliability()
    for f in filings:
        if partition_of(f) != "calibration":
            continue
        reviewed = by_key.get(f.key, [])
        if not reviewed or not f.model_checks or f.machine is None:
            continue
        expected = approved_values(latest(reviewed))
        for name, verdict in f.model_checks.items():
            if name not in expected:
                continue  # never looked at by a human
            machine = f.machine.values.get(name)
            kept = compare(expected[name], machine) in {"correct", "absent"}
            agree_seen, agree_right, disagree_seen, disagree_right = out.by_field.get(name, (0, 0, 0, 0))
            if verdict == "agree":
                out.agree_seen += 1
                out.agree_right += kept
                agree_seen += 1
                agree_right += kept
            elif verdict == "disagree":
                out.disagree_seen += 1
                out.disagree_right += not kept
                disagree_seen += 1
                disagree_right += not kept
            out.by_field[name] = (agree_seen, agree_right, disagree_seen, disagree_right)
    return out
