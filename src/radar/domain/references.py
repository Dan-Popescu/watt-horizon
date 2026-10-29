"""Resolve filing references without mixing administrative numbers across sources or regions."""

from collections.abc import Iterable, Mapping

from radar.domain.models import Filing


def canonical_reference_map[Reference](
    filings: Iterable[Filing], references: Mapping[str, Reference]
) -> dict[str, Reference]:
    records = list(filings)
    by_number: dict[str, list[str]] = {}
    known_keys = {filing.key for filing in records}
    for filing in records:
        by_number.setdefault(filing.number, []).append(filing.key)
    resolved: dict[str, Reference] = {}
    priority: dict[str, int] = {}
    for identity, reference in references.items():
        declared = getattr(reference, "filing_key", None)
        key = declared or identity
        explicit = key in known_keys or ":" in key
        if not explicit:
            candidates = by_number.get(key, [])
            if len(candidates) > 1:
                raise ValueError(f"Ambiguous legacy reference number {key!r}; qualify its filing_key")
            key = candidates[0] if candidates else key
        rank = 2 if explicit else 1
        if key in resolved:
            if rank == priority[key] and resolved[key] != reference:
                raise ValueError(f"Conflicting references for filing {key!r}")
            if rank < priority[key]:
                continue
        resolved[key] = reference
        priority[key] = rank
    return resolved
