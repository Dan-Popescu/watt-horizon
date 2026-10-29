"""Per-region source configuration. Adding a region = adding one `Region` entry (URLs observed on the sites)."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Region:
    slug: str
    departments: tuple[str, ...]
    dreal_base: str
    dreal_listings: tuple[str, ...]  # newest first; « avant-2025 » archives are very slow to page through
    mrae_pages: dict[int, str] = field(default_factory=dict)  # year -> « Avis rendus sur projets » page
    prefectures: dict[str, str] = field(default_factory=dict)  # department -> site root
    dreal_periods: dict[str, str] = field(default_factory=dict)  # observed listing -> annual/archive period


REGIONS: dict[str, Region] = {
    "hauts-de-france": Region(
        slug="hauts-de-france",
        departments=("02", "59", "60", "62", "80"),
        dreal_base="https://www.hauts-de-france.developpement-durable.gouv.fr/",
        dreal_listings=("2026-r5508.html", "2025-r5506.html", "avant-2025-r1468.html"),
        mrae_pages={
            2026: "https://www.mrae.developpement-durable.gouv.fr/avis-rendus-sur-projets-de-la-mrae-hauts-de-france-a1525.html"
        },
        prefectures={
            "59": "https://www.nord.gouv.fr",
            "62": "https://www.pas-de-calais.gouv.fr",
            "80": "https://www.somme.gouv.fr",
            "60": "https://www.oise.gouv.fr",
            "02": "https://www.aisne.gouv.fr",
        },
    ),
}


def get_region(slug: str, catalog_path: Path | None = None) -> Region:
    regions = REGIONS
    if catalog_path is not None:
        from radar.sources.catalog import configured_regions

        regions = configured_regions(catalog_path)
    try:
        return regions[slug]
    except KeyError:
        raise ValueError(f"Unknown region {slug!r}. Known: {', '.join(regions)}") from None
