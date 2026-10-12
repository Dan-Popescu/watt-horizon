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


def group_filings(filings: Iterable[Filing]) -> list[list[Filing]]:
    """Union-find over shared keys; groups and their members are sorted by filing key (stable output)."""
    items = sorted(filings, key=lambda f: f.key)
    parent = list(range(len(items)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, f in enumerate(items):
        for k in _keys(f):
            if k in owner:
                parent[root(i)] = root(owner[k])
            else:
                owner[k] = i
    groups: dict[int, list[Filing]] = {}
    for i, f in enumerate(items):
        groups.setdefault(root(i), []).append(f)
    return sorted(groups.values(), key=lambda g: g[0].key)
