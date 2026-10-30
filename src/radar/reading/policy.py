"""Versioned routing heuristics and budgets, independent of extraction decisions."""

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from radar.reading.ocr import OcrEngine
    from radar.reading.pdf import PdfContent

POLICY_VERSION = "adaptive-v1"
_USEFUL = re.compile(r"stockage|batter|datacenter|centre de donn|puissance|capacit|\bMW", re.I)
_UNCERTAIN = re.compile(r"sc.nario|variante|phase|tranche|existant|pr.vu|propos.|\ufffd", re.I)


@dataclass(frozen=True)
class ReadingPolicy:
    mode: Literal["economical", "adaptive", "full"] = "economical"
    max_deep_pages: int = 8
    max_ocr_pages: int = 8

    def __post_init__(self) -> None:
        if self.mode not in {"economical", "adaptive", "full"}:
            raise ValueError("Unknown reading policy mode")
        if min(self.max_deep_pages, self.max_ocr_pages) <= 0:
            raise ValueError("Adaptive reading page budgets must be positive")

    @property
    def identity(self) -> str:
        return f"{POLICY_VERSION}:{self.mode}:deep={self.max_deep_pages}:ocr={self.max_ocr_pages}"

    def reasons(self, text: str) -> tuple[str, ...]:
        return tuple(
            reason
            for pattern, reason in ((_USEFUL, "quantity_or_target_clue"), (_UNCERTAIN, "uncertain_context"))
            if pattern.search(text)
        )

    def table_pages(self, texts: tuple[str, ...]) -> tuple[tuple[int, ...], dict[int, tuple[str, ...]]]:
        reasons = {page: found for page, text in enumerate(texts, 1) if (found := self.reasons(text))}
        ranked = sorted(reasons, key=lambda page: (-len(reasons[page]), page))
        return tuple(sorted(ranked[: self.max_deep_pages])), reasons

    def describe(self) -> dict[str, object]:
        return {"version": POLICY_VERSION, **asdict(self)}


def complete_reading(
    document_id: str, data: bytes, content: "PdfContent", engine: "OcrEngine", policy: ReadingPolicy
) -> "PdfContent":
    """Route OCR by text clues under an explicit budget; every unvisited page stays visible.

    Empty scans have unknown usefulness and therefore remain candidates. No routing
    heuristic certifies that a page is irrelevant or that a transcription is correct.
    """
    if content.format != "pdf":
        return content
    if policy.mode != "adaptive":
        return engine.complete(document_id, data, content)
    scored: list[tuple[int, int]] = []
    for page in content.scanned_pages:
        neighborhood = " ".join(content.page_texts[max(0, page - 2) : min(content.pages, page + 1)])
        scored.append((-len(policy.reasons(neighborhood)), page))
    selected = tuple(sorted(page for _, page in sorted(scored)[: policy.max_ocr_pages]))
    withheld = tuple(page for page in content.scanned_pages if page not in selected)
    routed = replace(content, scanned_pages=selected)
    completed = engine.complete(document_id, data, routed)
    errors = dict(completed.ocr_errors)
    errors.update(dict.fromkeys(withheld, "Adaptive OCR page budget exceeded; usefulness remains unknown"))
    return replace(
        completed,
        scanned_pages=content.scanned_pages,
        ocr_errors=errors,
        useful_unread_pages=tuple(sorted(set(content.useful_unread_pages) | set(withheld))),
        reading_policy=policy.identity,
    )


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
