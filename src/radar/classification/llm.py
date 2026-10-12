"""Layer 2: Claude classifies with a closed taxonomy and must quote its justification (verified verbatim).

Optional dependency: `uv sync --extra llm`. Credentials: ANTHROPIC_API_KEY or an `ant auth login` profile.
Before sending text, natural-person names should already be minimised upstream (GDPR); only the first
`MAX_INPUT_CHARS` characters are sent (callers put the decision first: it is the shortest, clearest document).

A request is built once (`request`) and parsed once (`parse`), whether it is sent now (`classify`) or in a
Message Batch at half price (`pipeline/llm_batch.py`). The cache key is the request itself (`key`): only what is
actually sent decides whether a paid call is needed.
"""

import json
from typing import TYPE_CHECKING, Any

from radar.classification.base import ClassificationInput, Classifier
from radar.domain.enums import CATEGORY_LABELS_FR, Category
from radar.domain.models import Classification
from radar.domain.text import contains_quote
from radar.store.cache import cache_key

if TYPE_CHECKING:
    import anthropic

MAX_INPUT_CHARS = 12_000
PROMPT_VERSION = "2026-10-13"  # bump on any change to SYSTEM_PROMPT, OUTPUT_SCHEMA or the message layout
LLM_CATEGORIES = [c for c in Category if c not in {Category.NEEDS_REVIEW, Category.UNCLASSIFIED}]
SYSTEM_PROMPT = (
    "Tu classes des documents administratifs français (décisions et formulaires d'examen au cas par cas, arrêtés, "
    "avis) selon la nature du PROJET décrit. La citation doit être un extrait EXACT et court du document."
)
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": [c.value for c in LLM_CATEGORIES]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "quote": {"type": "string"},
    },
    "required": ["category", "confidence", "quote"],
    "additionalProperties": False,
}


def response_text(message: Any) -> str | None:
    """The JSON text of a Messages API response (sync or batch result), None on refusal or empty answer."""
    if getattr(message, "stop_reason", None) == "refusal":
        return None
    return next((block.text for block in message.content if block.type == "text"), None)


class LlmClassifier:
    NAMESPACE = "classification"

    def __init__(self, model: str, client: "anthropic.Anthropic | None" = None) -> None:
        self.model = model
        self._client = client
        self.name = f"llm:{model}@{PROMPT_VERSION}"

    @property
    def client(self) -> "anthropic.Anthropic":
        if self._client is None:
            import anthropic  # lazy: the package is an optional extra, and batch mode never needs a sync client

            self._client = anthropic.Anthropic()
        return self._client

    def request(self, item: ClassificationInput) -> dict[str, Any]:
        """Messages API parameters, identical for a direct call and a batch request."""
        categories = "\n".join(f"- {c.value} : {CATEGORY_LABELS_FR[c]}" for c in LLM_CATEGORIES)
        text = item.text[:MAX_INPUT_CHARS]
        return {
            "model": self.model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            "messages": [
                {"role": "user", "content": f"Catégories :\n{categories}\n\nTitre : {item.title}\n\nDocument :\n{text}"}
            ],
        }

    def key(self, item: ClassificationInput) -> str:
        return cache_key(self.name, json.dumps(self.request(item), sort_keys=True, ensure_ascii=False))

    def parse(self, message: Any, item: ClassificationInput) -> Classification | None:
        raw = response_text(message)
        if raw is None:
            return None
        out = json.loads(raw)
        return Classification(
            category=Category(out["category"]),
            method=self.name,
            confidence=out["confidence"],
            quote=out["quote"][:500],
            quote_verified=contains_quote(item.text, out["quote"]),
        )

    def classify(self, item: ClassificationInput) -> Classification | None:
        response = self.client.beta.messages.create(
            **self.request(item), betas=["server-side-fallback-2026-07-01"], fallbacks="default"
        )
        return self.parse(response, item)


class TieredClassifier:
    """A fast, cheap model first; the strong model only when the fast answer is doubtful (not « high », quote not
    found in the text, or no answer). OFF by default (`RADAR_LLM_FAST_MODEL`): a smaller model trades capability
    for cost, so it is switched on only after `radar eval --classifier` shows no loss."""

    def __init__(self, fast: Classifier, strong: Classifier) -> None:
        self.fast, self.strong = fast, strong
        self.name = f"tiered:{fast.name}>{strong.name}"

    def classify(self, item: ClassificationInput) -> Classification | None:
        first = self.fast.classify(item)
        if first is None and getattr(self.fast, "pending", False):
            return None  # batch mode: the fast answer is on its way; ask the strong model only if it is doubtful
        if first is not None and first.confidence == "high" and first.quote_verified:
            return first
        return self.strong.classify(item) or first
