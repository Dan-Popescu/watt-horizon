"""Locations in documents which do not have a PDF page model."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentSegment:
    locator: str
    text: str
    uncertain: bool = False
    reason: str = ""
    header: str = ""
    context: str = ""
