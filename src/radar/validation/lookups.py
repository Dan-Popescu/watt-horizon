"""Reference data used by checks and connectors, behind one port (`Lookups`).

- `ReferenceCommunes`: offline list of all French communes with centroids (geo.api.gouv.fr export, JSON list of
  {code, nom, codeDepartement, centre}). Answers `commune()` and `communes_in()` without network.
- `ApiLookups`: company register and point-in-commune, through a disk cache; with `offline=True` the cache only.
- `CompositeLookups`: the production combination. `StaticLookups`: tests.
URL formats are stable on purpose: they are the cache keys.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from radar.domain.text import fold
from radar.net.http import PoliteHttp


@dataclass(frozen=True)
class CompanyInfo:
    siren: str
    name: str
    naf: str | None
    created_on: str | None
    legal_form: str | None


@dataclass(frozen=True)
class CommuneInfo:
    insee_code: str
    name: str
    department: str
    lat: float | None = None
    lon: float | None = None


class Lookups(Protocol):
    def company(self, siren: str) -> CompanyInfo | None: ...
    def commune(self, name: str, department: str | None) -> CommuneInfo | None: ...
    def commune_at(self, lat: float, lon: float) -> CommuneInfo | None: ...
    def communes_in(self, department: str) -> list[CommuneInfo]: ...


def _to_commune(row: dict[str, Any]) -> CommuneInfo:
    coords = (row.get("centre") or {}).get("coordinates") or [None, None]
    return CommuneInfo(row["code"], row["nom"], row.get("codeDepartement") or "", lat=coords[1], lon=coords[0])


class ReferenceCommunes:
    def __init__(self, path: Path) -> None:
        rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.communes = [_to_commune(r) for r in rows if r.get("code") and r.get("nom")]
        self._by_name: dict[str, list[CommuneInfo]] = {}
        for c in self.communes:
            self._by_name.setdefault(fold(c.name), []).append(c)

    def __len__(self) -> int:
        return len(self.communes)

    def commune(self, name: str, department: str | None) -> CommuneInfo | None:
        matches = [c for c in self._by_name.get(fold(name), []) if department in (None, c.department)]
        return matches[0] if len(matches) == 1 else None  # ambiguous homonyms need a department

    def communes_in(self, department: str) -> list[CommuneInfo]:
        return [c for c in self.communes if c.department == department]


class JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # atomic on the same volume

    def merge(self, other: dict[str, Any]) -> int:
        """Add entries we do not have; failed responses (`{"_error": ...}`) are never cached."""
        added = {k: v for k, v in other.items() if k not in self.data and not (isinstance(v, dict) and "_error" in v)}
        if added:
            self.data.update(added)
            self.save()
        return len(added)


class ApiLookups:
    COMPANY = "https://recherche-entreprises.api.gouv.fr/search?q={siren}"
    COMMUNE_AT = "https://geo.api.gouv.fr/communes?lat={lat}&lon={lon}&fields=code,nom"

    def __init__(self, http: PoliteHttp | None, cache: JsonCache, offline: bool = False) -> None:
        self.http, self.cache, self.offline = http, cache, offline

    def _json(self, url: str) -> Any:
        cached = self.cache.get(url)
        if cached is not None or self.offline or self.http is None:
            return cached
        data = self.http.get_json(url, min_interval_s=0.25)
        self.cache.put(url, data)
        return data

    def company(self, siren: str) -> CompanyInfo | None:
        results = (self._json(self.COMPANY.format(siren=siren)) or {}).get("results") or []
        hit = next((r for r in results if r.get("siren") == siren), None)
        if hit is None:
            return None
        return CompanyInfo(
            siren,
            hit.get("nom_complet") or "",
            hit.get("activite_principale"),
            hit.get("date_creation"),
            str(hit.get("nature_juridique") or "") or None,
        )

    def commune_at(self, lat: float, lon: float) -> CommuneInfo | None:
        rows = self._json(self.COMMUNE_AT.format(lat=lat, lon=lon)) or []
        return (
            CommuneInfo(rows[0]["code"], rows[0]["nom"], rows[0].get("codeDepartement") or rows[0]["code"][:2])
            if rows
            else None
        )


class CompositeLookups:
    def __init__(self, reference: ReferenceCommunes, api: ApiLookups) -> None:
        self.reference, self.api = reference, api

    def company(self, siren: str) -> CompanyInfo | None:
        return self.api.company(siren)

    def commune(self, name: str, department: str | None) -> CommuneInfo | None:
        return self.reference.commune(name, department)

    def commune_at(self, lat: float, lon: float) -> CommuneInfo | None:
        return self.api.commune_at(lat, lon)

    def communes_in(self, department: str) -> list[CommuneInfo]:
        return self.reference.communes_in(department)


class StaticLookups:
    """In-memory lookups for tests."""

    def __init__(
        self,
        companies: dict[str, CompanyInfo] | None = None,
        communes: list[CommuneInfo] | None = None,
        at: dict[tuple[float, float], CommuneInfo] | None = None,
    ) -> None:
        self.companies, self.communes, self.at = companies or {}, communes or [], at or {}

    def company(self, siren: str) -> CompanyInfo | None:
        return self.companies.get(siren)

    def commune(self, name: str, department: str | None) -> CommuneInfo | None:
        return next(
            (c for c in self.communes if fold(c.name) == fold(name) and department in (None, c.department)), None
        )

    def commune_at(self, lat: float, lon: float) -> CommuneInfo | None:
        return self.at.get((round(lat, 3), round(lon, 3)))

    def communes_in(self, department: str) -> list[CommuneInfo]:
        return [c for c in self.communes if c.department == department]
