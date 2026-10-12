"""Layer 2: Claude classifies with a closed taxonomy and must quote its justification (verified verbatim).

Optional dependency: `uv sync --extra llm`. Credentials: ANTHROPIC_API_KEY or an `ant auth login` profile.
Before sending text, natural-person names should already be minimised upstream (GDPR); only the first
`MAX_INPUT_CHARS` characters are sent.
"""

import json
from typing import TYPE_CHECKING, Any

from radar.classification.base import ClassificationInput
from radar.domain.enums import CATEGORY_LABELS_FR, Category
from radar.domain.models import Classification
from radar.domain.text import contains_quote

if TYPE_CHECKING:
    import anthropic

MAX_INPUT_CHARS = 12_000
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


class LlmClassifier:
    def __init__(self, model: str, client: "anthropic.Anthropic | None" = None) -> None:
        import anthropic  # lazy: the package is an optional extra

        self.model = model
        self.client = client or anthropic.Anthropic()
        self.name = f"llm:{model}"

    def classify(self, item: ClassificationInput) -> Classification | None:
        categories = "\n".join(f"- {c.value} : {CATEGORY_LABELS_FR[c]}" for c in LLM_CATEGORIES)
        text = item.text[:MAX_INPUT_CHARS]
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            messages=[
                {"role": "user", "content": f"Catégories :\n{categories}\n\nTitre : {item.title}\n\nDocument :\n{text}"}
            ],
        )
        if response.stop_reason == "refusal":
            return None
        raw = next((block.text for block in response.content if block.type == "text"), None)
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
