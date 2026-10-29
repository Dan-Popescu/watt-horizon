"""Fallback extraction by an LLM, only for fields the rules did not find, only on target filings.

Trust comes from verification, not from the model: a value is kept only if its quote is found verbatim in the
(GDPR-scrubbed) document text AND the value itself appears in that quote. Anything else is dropped.
Optional dependency: `uv sync --extra llm`. Cached through `CachedExtractor` (same input -> no new call).
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from math import isclose
from typing import TYPE_CHECKING, Any, Protocol

from radar.domain.ids import luhn_ok
from radar.domain.quantities import measurements, normalized_value
from radar.domain.text import contains_quote, fold, squash
from radar.store.cache import KeyValueCache, cache_key

if TYPE_CHECKING:
    import anthropic

PROMPT_VERSION = "2026-10-29"  # bump on any change to SYSTEM_PROMPT, FIELD_HINTS or OUTPUT_SCHEMA
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
    " Respecte les unités : MW est une puissance et MWh une capacité. Cite uniquement le projet concerné, "
    "pas un exemple ou un équipement existant. Pour la puissance, retiens la borne maximale (MWac si indiqué) ; "
    "pour la date de dépôt, ne cite jamais la date de décision. Les valeurs numériques sont normalisées en MW ou MWh."
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
        return normalized_value(raw, "energy" if name == "energy_mwh" else "power")
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
        dimension = "energy" if name == "energy_mwh" else "power"
        candidates = [m for m in measurements(quote) if m.dimension == dimension and m.basis not in {"dc", "c"}]
        if not candidates or re.search(
            r"\b(?:existant\w*|actuel\w*|ancien\w*|transformateur\w*|hypotheti\w*|exemple|autre projet|projet voisin)\b",
            fold(quote),
        ):
            return False
        expected = [m.low if name == "power_mw_min" else m.high for m in candidates]
        # A quote with conflicting values of the same dimension does not establish one unambiguous scalar.
        return bool(expected) and all(isclose(n, value, rel_tol=1e-8, abs_tol=1e-10) for n in expected)
    if name == "siret":
        return str(value) in re.sub(r"\D", "", quote)
    if name == "filed_on":
        if not re.search(r"\b(?:depos\w*|depot|complet\w*|recu\w*)\b", fold(quote)):
            return False
        if re.search(r"\b(?:decision|arrete|deliberation)\b", fold(quote)):
            return False
        y, m, d = str(value).split("-")
        digits = re.sub(r"\D", "", quote)
        written = re.search(rf"\b0?{int(d)}(?:er)?\s+{MONTHS_FR[int(m) - 1]}\s+{y}\b", fold(quote))
        return f"{d}{m}{y}" in digits or f"{y}{m}{d}" in digits or written is not None
    return fold(str(value)) in fold(quote)


def _source_contexts(text: str, quote: str) -> list[str]:
    """Preserve the containing sentence, including units or object qualifiers omitted by a short quote."""
    text, quote = squash(text), squash(quote)
    normalized = text.lower()
    start = normalized.find(quote.lower())
    contexts = []
    while start >= 0:
        end = start + len(quote)
        before = list(re.finditer(r"[.;](?:\s|$)", text[:start]))
        after = re.search(r"[.;](?:\s|$)", text[end:])
        left = before[-1].end() if before else 0
        right = end if re.search(r"[.;]\s*$", quote) else end + after.start() if after else len(text)
        contexts.append(text[left:right])
        start = normalized.find(quote.lower(), end)
    return contexts


def verify(answer: dict[str, Any], item: ExtractionInput) -> Extracted:
    kept: Extracted = {}
    for name in item.fields:
        entry = answer.get(name) or {}
        if not isinstance(entry, dict):
            continue
        raw, quote = entry.get("value"), entry.get("quote")
        if not raw or not isinstance(quote, str) or not contains_quote(item.text, quote):
            continue
        value = parse_value(name, str(raw))
        if value is not None and value_in_quote(name, value, quote):
            if isinstance(value, float) and not all(
                value_in_quote(name, value, context) for context in _source_contexts(item.text, quote)
            ):
                continue
            kept[name] = (value, quote)  # Truncating can remove the very value/unit whose presence was verified.
    return kept


# Passage selection (opt-in, `RADAR_LLM_PASSAGES=1`): instead of the first MAX_INPUT_CHARS, send only windows
# around the clues of the missing fields. ~60 % fewer tokens; to be switched on after `radar eval` shows no recall loss.
CLUES: dict[str, str] = {
    "developer": r"soci[ée]t[ée]|d[ée]nomination|porteur|p[ée]titionnaire|ma[îi]tre d.ouvrage",
    "siret": r"siret|siren",
    "commune": r"commune|lieu-dit|localit[ée]",
    "power_mw": r"\bMW|puissance",
    "power_mw_min": r"\bMW|puissance",
    "energy_mwh": r"MWh|capacit[ée]",
    "substation": r"poste|raccord",
    "filed_on": r"d[ée]pos[ée]|complet le|re[çc]u le",
}
WINDOW = 400


def select_passages(text: str, fields: tuple[str, ...], budget: int = MAX_INPUT_CHARS // 2) -> str:
    """Merged windows of ±WINDOW chars around every clue of the requested fields, in document order."""
    spans: list[tuple[int, int]] = []
    for name in fields:
        for m in re.finditer(CLUES.get(name, r"$^"), text, re.IGNORECASE):
            spans.append((max(0, m.start() - WINDOW), min(len(text), m.end() + WINDOW)))
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = " […] ".join(text[a:b] for a, b in merged)
    return out[:budget] if out else text[:budget]


class LlmExtractor:
    NAMESPACE = "extraction"

    def __init__(self, model: str, client: "anthropic.Anthropic | None" = None, passages: bool = False) -> None:
        self.model, self._client, self.passages = model, client, passages
        self.name = f"llm-extract:{model}@{PROMPT_VERSION}" + (":passages" if passages else "")

    @property
    def client(self) -> "anthropic.Anthropic":
        if self._client is None:
            import anthropic  # lazy: optional extra

            self._client = anthropic.Anthropic()
        return self._client

    def request(self, item: ExtractionInput) -> dict[str, Any]:
        asked = "\n".join(f"- {name} : {FIELD_HINTS[name]}" for name in item.fields)
        text = select_passages(item.text, item.fields) if self.passages else item.text[:MAX_INPUT_CHARS]
        return {
            "model": self.model,
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": output_schema(item.fields)}},
            "messages": [
                {"role": "user", "content": f"Champs :\n{asked}\n\nTitre : {item.title}\n\nDocument :\n{text}"}
            ],
        }

    def key(self, item: ExtractionInput) -> str:
        return cache_key(self.name, json.dumps(self.request(item), sort_keys=True, ensure_ascii=False))

    def parse(self, message: Any, item: ExtractionInput) -> Extracted:
        if getattr(message, "stop_reason", None) == "refusal":
            return {}
        raw = next((block.text for block in message.content if block.type == "text"), None)
        return verify(json.loads(raw), item) if raw else {}

    def extract(self, item: ExtractionInput) -> Extracted:
        if not item.fields:
            return {}
        response = self.client.beta.messages.create(
            **self.request(item), betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
        return self.parse(response, item)


class CachedExtractor:
    NAMESPACE = "extraction"

    def __init__(self, inner: FieldExtractor, cache: KeyValueCache) -> None:
        self.inner, self.cache = inner, cache
        self.name = inner.name

    def extract(self, item: ExtractionInput) -> Extracted:
        if not item.fields:
            return {}
        key = (
            self.inner.key(item)
            if hasattr(self.inner, "key")
            else cache_key(self.name, ",".join(item.fields), item.title, item.text)
        )
        if (hit := self.cache.get(self.NAMESPACE, key)) is not None:
            return {name: (value, quote) for name, (value, quote) in hit.items()}
        result = self.inner.extract(item)
        self.cache.put(self.NAMESPACE, key, {name: [value, quote] for name, (value, quote) in result.items()})
        return result
