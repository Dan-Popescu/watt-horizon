"""Which filings describe the same physical project (e.g. a DREAL request, then an MRAe opinion, then a permit).

Merging is conservative: a wrong merge hides a project, a missed merge only shows a duplicate (and the linker
still proposes it as related). Two target filings are merged only when they share the same commune (INSEE code,
else folded name) AND either the same SIRET or the same developer (folded name).
A SIRET alone is not enough: one company often files several projects (e.g. DECADE ENERGY ASSETS for Méru and
Morcourt, APAL MW for Aubenton and Bonneuil-les-Eaux, observed 2026-10).
"""

from collections.abc import Iterable

from radar.domain.models import Filing
from radar.domain.text import fold


def _keys(f: Filing) -> list[str]:
    place = f.location.insee_code or fold(f.location.commune)
    if not place:
        return []
    keys = [f"siret-place:{f.siret}|{place}"] if f.siret else []
    developer = fold(f.developer)
    if developer and "masque" not in developer:
        keys.append(f"developer-place:{developer}|{place}")
    return keys


def group_filings(
    filings: Iterable[Filing],
    must: Iterable[tuple[str, str]] = (),
    cannot: Iterable[tuple[str, str]] = (),
) -> list[list[Filing]]:
    """Union-find over shared keys; groups and their members are sorted by filing key (stable output).
    Human decisions win: `must` pairs (filing keys) are merged first, and no merge ever joins a `cannot` pair."""
    items = sorted(filings, key=lambda f: f.key)
    index = {f.key: i for i, f in enumerate(items)}
    parent = list(range(len(items)))
    members: dict[int, set[int]] = {i: {i} for i in range(len(items))}
    forbidden = {(index[a], index[b]) for a, b in cannot if a in index and b in index}
    forbidden |= {(j, i) for i, j in forbidden}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = root(i), root(j)
        if ri == rj or any((x, y) in forbidden for x in members[ri] for y in members[rj]):
            return
        parent[ri] = rj
        members[rj] |= members.pop(ri)

    for a, b in must:
        if a in index and b in index:
            union(index[a], index[b])
    owner: dict[str, int] = {}
    for i, f in enumerate(items):
        for k in _keys(f):
            if k in owner:
                union(i, owner[k])
            else:
                owner[k] = i
    groups: dict[int, list[Filing]] = {}
    for i, f in enumerate(items):
        groups.setdefault(root(i), []).append(f)
    return sorted(groups.values(), key=lambda g: g[0].key)
