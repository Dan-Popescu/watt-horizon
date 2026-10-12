"""Explainable linking rules. A link is a proposal with reasons; it never merges records by itself.

Rules:
- same commune (folded) between any two records;
- same distinctive word in developer names (generic words such as "energy" or "storage" are ignored);
- same substation name between two filings.
Press-to-press links are skipped (articles often cite many places).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations

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


def propose_links(filings: Iterable[Filing], signals: Iterable[Signal]) -> list[Link]:
    nodes = [_from_filing(f) for f in filings] + [_from_signal(s) for s in signals]
    links: list[Link] = []
    for a, b in combinations(nodes, 2):
        if a.source is SourceKind.PRESS and b.source is SourceKind.PRESS:
            continue
        reasons: list[str] = []
        if shared := a.communes & b.communes:
            reasons.append("même commune : " + ", ".join(sorted(shared)))
        if shared := a.developer_words & b.developer_words:
            reasons.append("même nom de porteur : " + ", ".join(sorted(shared)))
        if a.substation and a.substation == b.substation:
            reasons.append(f"même poste visé : {a.substation}")
        if reasons:
            first, second = sorted((a.key, b.key))
            links.append(Link(a=first, b=second, reasons=tuple(reasons)))
    return links
