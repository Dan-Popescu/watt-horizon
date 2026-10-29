"""Conservative, page-local quantities in opinions and annexes; ambiguity means abstention."""

import re
from collections.abc import Iterable
from math import isclose

from radar.domain.models import Evidence
from radar.domain.quantities import measurements
from radar.domain.text import fold, squash
from radar.extraction.facts import Facts
from radar.reading.pdf import PdfContent

_TARGET = re.compile(r"\b(?:stockage|batteries?|bess|datacenter|data center|centre de donnees)\b")
_ROLE = re.compile(r"\b(?:projet|prevu\w*|propose\w*|installe\w*)\b")
_AMBIGUOUS = re.compile(
    r"\b(?:existant\w*|actuel\w*|ancien\w*|exemple|transformateur\w*|voisin\w*|"
    r"scenario\w*|variante\w*|option\w*|phase\w*|tranche\w*|raccordement|photovolta\w*|solaire|eolien\w*)\b"
)


def extract_supporting(documents: Iterable[tuple[str, PdfContent]]) -> Facts:
    """Only explicit project quantities agreeing across every eligible supporting passage.

    Does not interpret technical tables, aggregate equipment, choose phases or resolve contradictions.
    A shared power evidence entry must support both MW and MWh when both are retained.
    """
    candidates: list[tuple[float | None, float | None, float | None, Evidence]] = []
    for document_id, content in documents:
        for page in range(1, content.pages + 1):
            text = content.ocr_texts.get(page) or content.page_texts[page - 1]
            for raw in re.split(r"(?<=[.;])\s+|\n\s*\n", text):
                quote = squash(raw)
                normalized = fold(quote)
                if (
                    not 0 < len(quote) <= 550
                    or not _TARGET.search(normalized)
                    or not _ROLE.search(normalized)
                    or _AMBIGUOUS.search(normalized)
                ):
                    continue
                values = measurements(quote)
                if any(value.basis in {"dc", "c"} for value in values):
                    continue
                powers = {(value.low, value.high) for value in values if value.dimension == "power"}
                energies = {(value.low, value.high) for value in values if value.dimension == "energy"}
                if len(powers) > 1 or len(energies) > 1:
                    # Discarding the conflicting statement would let a later,
                    # simpler repetition conceal the contradiction.
                    return Facts()
                if not (powers or energies):
                    continue
                low, high = next(iter(powers), (None, None))
                energy_low, energy = next(iter(energies), (None, None))
                if energy_low != energy or (high is not None and not 0 < high <= 3000):
                    continue
                if energy is not None and not 0 < energy < 100_000:
                    continue
                candidates.append(
                    (low, high, energy, Evidence(document_id=document_id, locator=f"page:{page}", quote=quote))
                )
    facts = Facts()
    quantities = [(low, high, energy) for low, high, energy, _ in candidates]
    for index, attr in enumerate(("mw_min", "mw_max", "mwh")):
        present = [value for row in quantities if (value := row[index]) is not None]
        if present and all(isclose(value, present[0], rel_tol=1e-8, abs_tol=1e-10) for value in present):
            setattr(facts, attr, present[0])
    # A contradictory power makes the supporting project statement ambiguous as a whole.
    if any(candidate[1] is not None for candidate in candidates) and facts.mw_max is None:
        return Facts()
    for low, high, energy, evidence in candidates:
        if (low, high, energy) == (facts.mw_min, facts.mw_max, facts.mwh):
            facts.evidence["power"] = evidence
            return facts
    # Never assemble a quantity pair with a citation proving only one component.
    return Facts()
