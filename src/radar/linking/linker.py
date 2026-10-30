"""Explainable linking rules. A link is a proposal with reasons; it never merges records by itself.

Rules:
- same commune (folded) between any two records;
- same distinctive word in developer names (generic words such as "energy" or "storage" are ignored);
- same substation name between two filings.
Only pairs involving at least one filing are compared (signal-to-signal links are never used), and installations
from the national register are matched by the strict rule in `linking/registry.py` instead.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from radar.domain.enums import SourceKind
from radar.domain.models import Filing, Link, Signal
from radar.domain.text import fold

GENERIC_WORDS = frozenset(
    {
        "energy",
        "energie",
        "energies",
        "storage",
        "stockage",
        "assets",
        "france",
        "data",
        "center",
        "centre",
        "solar",
        "power",
        "societe",
        "electrique",
        "poste",
        "chemin",
        "group",
        "groupe",
        "industries",
        "industry",
        "sas",
        "sarl",
        "reseau",
        "transport",
        "electricite",
        "projet",
        "personne",
        "physique",
        "masque",
    }
)


@dataclass(frozen=True)
class _Node:
    key: str
    source: SourceKind
    developer_words: frozenset[str]
    communes: frozenset[str]
    substation: str


def distinctive_words(name: str | None) -> frozenset[str]:
    return frozenset(w for w in fold(name).split() if len(w) >= 4 and w not in GENERIC_WORDS and not w.isdigit())


def _from_filing(f: Filing) -> _Node:
    communes = frozenset({fold(f.location.commune)} - {""})
    return _Node(
        f.key, f.source, distinctive_words(f.developer) | distinctive_words(f.legal_name), communes, fold(f.substation)
    )


def _from_signal(s: Signal) -> _Node:
    return _Node(s.key, s.source, distinctive_words(s.developer), frozenset(fold(c) for c in s.communes if c), "")


def _keys(node: _Node) -> Iterable[tuple[str, str]]:
    yield from (("commune", value) for value in node.communes)
    yield from (("developer", value) for value in node.developer_words)
    if node.substation:
        yield "substation", node.substation


def _index(nodes: list[_Node], only_keys: set[str] | None = None) -> dict[tuple[str, str], set[int]]:
    index: dict[tuple[str, str], set[int]] = {}
    for position, node in enumerate(nodes):
        if only_keys is not None and node.key not in only_keys:
            continue
        for key in _keys(node):
            index.setdefault(key, set()).add(position)
    return index


def _candidates(node: _Node, index: dict[tuple[str, str], set[int]]) -> list[int]:
    candidates: set[int] = set()
    for key in _keys(node):
        candidates.update(index.get(key, ()))
    return sorted(candidates)


def _link(a: _Node, b: _Node) -> Link:
    reasons: list[str] = []
    if shared := a.communes & b.communes:
        reasons.append("même commune : " + ", ".join(sorted(shared)))
    if shared := a.developer_words & b.developer_words:
        reasons.append("même nom de porteur : " + ", ".join(sorted(shared)))
    if a.substation and a.substation == b.substation:
        reasons.append(f"même poste visé : {a.substation}")
    first, second = sorted((a.key, b.key))
    return Link(a=first, b=second, reasons=tuple(reasons))


def iter_propose_links(
    filings: Iterable[Filing],
    signals: Iterable[Signal],
    *,
    only_keys: set[str] | None = None,
    metrics: dict[str, int] | None = None,
) -> Iterator[Link]:
    """Compare only shared-key candidates; retain every dense match and the original stable order."""
    filing_nodes = [_from_filing(f) for f in filings]
    signal_nodes = [_from_signal(s) for s in signals if s.source is not SourceKind.ENERGY_REGISTRY]
    filing_index, signal_index = _index(filing_nodes), _index(signal_nodes)
    touched_filings = _index(filing_nodes, only_keys) if only_keys is not None else filing_index
    touched_signals = _index(signal_nodes, only_keys) if only_keys is not None else signal_index
    compared = 0
    for i, node in enumerate(filing_nodes):
        index = filing_index if only_keys is None or node.key in only_keys else touched_filings
        for j in _candidates(node, index):
            if j > i:
                yield _link(node, filing_nodes[j])
                compared += 1
    for node in filing_nodes:
        index = signal_index if only_keys is None or node.key in only_keys else touched_signals
        for j in _candidates(node, index):
            yield _link(node, signal_nodes[j])
            compared += 1
    if metrics is not None:
        metrics["candidate_pairs"] = compared


def propose_links(
    filings: Iterable[Filing],
    signals: Iterable[Signal],
    *,
    only_keys: set[str] | None = None,
    metrics: dict[str, int] | None = None,
) -> list[Link]:
    """Compatibility list result; production graph publication consumes iter_propose_links."""
    return list(iter_propose_links(filings, signals, only_keys=only_keys, metrics=metrics))
