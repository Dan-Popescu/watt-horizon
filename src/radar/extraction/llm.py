"""Fallback extraction by an LLM, only for fields the rules did not find, only on target filings.

Trust comes from verification, not from the model: a value is kept only if its quote is found verbatim in the
(GDPR-scrubbed) document text AND the value itself appears in that quote. Anything else is dropped.
Optional dependency: `uv sync --extra llm`. Cached through `CachedExtractor` (same input -> no new call).
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

from radar.domain.ids import luhn_ok
from radar.domain.text import contains_quote, fold, parse_number
from radar.store.cache import KeyValueCache, cache_key

if TYPE_CHECKING:
    import anthropic

PROMPT_VERSION = "2026-10-12"  # bump on any change to SYSTEM_PROMPT, FIELD_HINTS or OUTPUT_SCHEMA
MAX_INPUT_CHARS = 20_000
FIELD_HINTS: dict[str, str] = {
    "developer": "société qui porte le projet (dénomination), jamais une personne physique",
    "siret": "numéro SIRET à 14 chiffres de cette société",
    "commune": "commune d'implantation du projet",
    "power_mw": "puissance électrique maximale en MW (nombre)",
    "power_mw_min": "puissance minimale en MW si une fourchette est donnée (nombre)",
    "energy_mwh": "capacité de stockage en MWh (nombre)",
    "substation": "nom du poste électrique de raccordement (RTE/Enedis)",
    "filed_on": "date de dépôt du dossier complet (AAAA-MM-JJ)",
}
SYSTEM_PROMPT = (
    "Tu extrais des informations de documents administratifs français. Pour chaque champ demandé, donne la valeur "
    "et une citation EXACTE et courte du document qui la contient. Si l'information n'est pas dans le document, "
    "réponds null pour la valeur et la citation. N'invente rien, ne déduis rien."
)
MONTHS_FR = (
    "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
)  # fmt: skip
Extracted = dict[str, tuple[str | float, str]]  # field -> (value, verified quote)


@dataclass(frozen=True)
class ExtractionInput:
    filing_key: str
    title: str
    text: str  # GDPR-scrubbed document texts
    fields: tuple[str, ...]  # missing fields to look for


class FieldExtractor(Protocol):
    name: str

    def extract(self, item: ExtractionInput) -> Extracted: ...


def output_schema(fields: tuple[str, ...]) -> dict[str, Any]:
    entry = {
        "type": "object",
        "properties": {"value": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
        "required": ["value", "quote"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": dict.fromkeys(fields, entry),
        "required": list(fields),
        "additionalProperties": False,
    }


def parse_value(name: str, raw: str) -> str | float | None:
    """Typed value, or None when it does not parse (the answer is then dropped)."""
    raw = raw.strip()
    if name in {"power_mw", "power_mw_min", "energy_mwh"}:
        m = re.search(r"\d[\d\s  ]*(?:[.,]\d+)?", raw)
        value = parse_number(m[0]) if m else 0.0
        return value if 0 < value < 100_000 else None
    if name == "siret":
        digits = re.sub(r"\D", "", raw)
        return digits if len(digits) == 14 and luhn_ok(digits) else None
    if name == "filed_on":
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            return None
    return raw or None


def value_in_quote(name: str, value: str | float, quote: str) -> bool:
    if isinstance(value, float):
        numbers = [parse_number(m) for m in re.findall(r"\d[\d  ]*(?:[.,]\d+)?", quote)]
        return any(abs(n - value) < 1e-6 for n in numbers)
    if name == "siret":
        return str(value) in re.sub(r"\D", "", quote)
    if name == "filed_on":
        y, m, d = str(value).split("-")
        digits = re.sub(r"\D", "", quote)
        written = re.search(rf"\b0?{int(d)}(?:er)?\s+{MONTHS_FR[int(m) - 1]}\s+{y}\b", fold(quote))
        return f"{d}{m}{y}" in digits or f"{y}{m}{d}" in digits or written is not None
    return fold(str(value)) in fold(quote)


def verify(answer: dict[str, Any], item: ExtractionInput) -> Extracted:
    kept: Extracted = {}
    for name in item.fields:
        entry = answer.get(name) or {}
        raw, quote = entry.get("value"), entry.get("quote")
        if not raw or not quote or not contains_quote(item.text, quote):
            continue
        value = parse_value(name, str(raw))
        if value is not None and value_in_quote(name, value, quote):
            kept[name] = (value, quote[:300])
    return kept


class LlmExtractor:
    def __init__(self, model: str, client: "anthropic.Anthropic | None" = None) -> None:
        if client is None:
            import anthropic  # lazy: optional extra

            client = anthropic.Anthropic()
        self.model, self.client = model, client
        self.name = f"llm-extract:{model}@{PROMPT_VERSION}"

    def extract(self, item: ExtractionInput) -> Extracted:
        if not item.fields:
            return {}
        asked = "\n".join(f"- {name} : {FIELD_HINTS[name]}" for name in item.fields)
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=2048,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": output_schema(item.fields)}},
            messages=[
                {
                    "role": "user",
                    "content": f"Champs :\n{asked}\n\nTitre : {item.title}\n\nDocument :\n{item.text[:MAX_INPUT_CHARS]}",
                }
            ],
        )
        if response.stop_reason == "refusal":
            return {}
        raw = next((block.text for block in response.content if block.type == "text"), None)
        return verify(json.loads(raw), item) if raw else {}


class CachedExtractor:
    NAMESPACE = "extraction"

    def __init__(self, inner: FieldExtractor, cache: KeyValueCache) -> None:
        self.inner, self.cache = inner, cache
        self.name = inner.name

    def extract(self, item: ExtractionInput) -> Extracted:
        key = cache_key(self.name, ",".join(item.fields), item.title, item.text)
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            return {name: (value, quote) for name, (value, quote) in hit.items()}
        result = self.inner.extract(item)
        self.cache.put(self.NAMESPACE, key, {name: [value, quote] for name, (value, quote) in result.items()})
        return result
