"""Discovery recall on a manually assembled external inventory, including never-collected cases."""

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urldefrag, urlparse

from pydantic import BaseModel, ConfigDict

from radar.domain.enums import SourceKind
from radar.evaluation.stats import wilson
from radar.store.db import Database

SOURCES = {
    "dreal": SourceKind.DREAL_CASE_BY_CASE,
    "mrae": SourceKind.MRAE_OPINION,
    "prefectures": SourceKind.PREFECTURE,
}


class DiscoveryReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    project_group: str
    source: Literal["dreal", "mrae", "prefectures"]
    region: str
    dossier_url: str
    document_urls: tuple[str, ...] = ()
    relevant: bool
    basis: Literal["manual_inventory"] = "manual_inventory"
    reviewer: str = ""
    reviewed_on: str = ""
    verified: bool = False


def canonical(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Inventory requires absolute public document URLs without credentials")
    return urldefrag(url)[0]


def evaluate(db: Database, path: Path) -> dict[str, Any]:
    """The inventory sampling protocol, human labels and its completeness remain external facts.

    A found case requires downloaded document evidence, not merely a successful HTML request.
    No source-wide precision or Internet-wide recall is inferred from this convenience set.
    """
    references = [
        DiscoveryReference.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    identities: set[tuple[str, str, str]] = set()
    cells: dict[str, list[tuple[DiscoveryReference, bool, str, int, int]]] = defaultdict(list)
    for reference in references:
        root = canonical(reference.dossier_url)
        identity = (reference.source, reference.region, root)
        if identity in identities:
            raise ValueError("Duplicate source/region/dossier reference")
        identities.add(identity)
        expected_urls = {canonical(url) for url in reference.document_urls}
        if len(expected_urls) != len(reference.document_urls):
            raise ValueError("Duplicate expected document URL")
        if not reference.verified:
            continue
        if not reference.reviewer.strip() or not reference.project_group.strip() or not reference.region.strip():
            raise ValueError("Verified inventory requires an explicit reviewer, project group and region")
        date.fromisoformat(reference.reviewed_on)
        prefix = f"{SOURCES[reference.source].value}:{reference.region}:"
        keys = [key for key in db.dossier_keys_for_root(root) if key.startswith(prefix)]
        found_urls: set[str] = set()
        for key in keys:
            found_urls.update(canonical(document.url) for document in db.documents(key))
            found_urls.update(
                canonical(edge["url"])
                for edge in db.dossier_edges(key)
                if edge["document_id"] and edge["state"] == "document"
            )
        found = bool(found_urls)
        listed = db.listing_item_status(root)
        stage = "downloaded" if found else "listed_not_downloaded" if listed else "never_observed"
        cells[reference.source].append((reference, found, stage, len(expected_urls & found_urls), len(expected_urls)))
    by_source = {}
    for source, items in sorted(cells.items()):
        positives = [item for item in items if item[0].relevant]
        groups: dict[str, bool] = {}
        for reference, found, _, _, _ in positives:
            groups[reference.project_group] = groups.get(reference.project_group, True) and found
        found_n = sum(item[1] for item in positives)
        expected_documents = sum(item[4] for item in positives)
        found_documents = sum(item[3] for item in positives)
        by_source[source] = {
            "verified_cases": len(items),
            "relevant_cases": len(positives),
            "found_relevant_cases": found_n,
            "case_recall": found_n / len(positives) if positives else None,
            "independent_groups": len(groups),
            "independent_group_recall": sum(groups.values()) / len(groups) if groups else None,
            "independent_group_ci95": wilson(sum(groups.values()), len(groups)),
            "expected_documents": expected_documents,
            "found_expected_documents": found_documents,
            "expected_document_recall": found_documents / expected_documents if expected_documents else None,
            "missing_case_stages": {
                stage: sum(item[2] == stage for item in positives)
                for stage in ("listed_not_downloaded", "never_observed")
            },
            "found_known_irrelevant_cases": sum(item[1] and not item[0].relevant for item in items),
        }
    measured = any(item["relevant_cases"] for item in by_source.values())
    return {
        "status": "measured" if measured else "unmeasured",
        "quality_measured": measured,
        "references": len(references),
        "verified_references": sum(reference.verified for reference in references),
        "scope": "manual_reference_inventory_only",
        "by_source": by_source,
        "caveat": "Rappel sur l’inventaire humain fourni ; aucun taux d’exhaustivité global déduit.",
    }
