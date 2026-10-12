"""Géorisques classified-installations API: a verification source more than a detection source.

Pitfalls observed: `codeNaf` uses the OLD nomenclature (division 63 = transport support/warehousing, not data
hosting), and data centers hosted on another company's site do not appear under their own name.
"""

import re
from typing import Any

from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.models import Signal
from radar.net.http import PoliteHttp
from radar.sources.base import CollectReport
from radar.store.db import Database

API = "https://georisques.gouv.fr/api/v1/installations_classees?departement={dep}&page_size=1000&page={page}"
SITE = "https://www.georisques.gouv.fr/risques/installations/donnees/details/{aiot}"
DATACENTER_NAME = re.compile(
    r"data ?cent|datacenter|centre de donn|h(?:é|e)bergement (?:de donn|informatique)|\bcloud\b|digital realty|equinix"
    r"|interxion|telehouse|\bdata4\b|\bovh\b|nebius|sesterce|azur data|scaleway|\bcolt\b|\bntt\b",
    re.IGNORECASE,
)
MIN_BATTERY_KW = 1000.0


def to_signal(row: dict[str, Any], department: str) -> Signal | None:
    name = row.get("raisonSociale") or ""
    rubriques = {r.get("numeroRubrique"): r for r in row.get("rubriques") or []}
    payload = {
        "regime": str(row.get("regime") or ""),
        "etat": str(row.get("etatActivite") or ""),
        "rubriques": ",".join(sorted(k for k in rubriques if k)),
        "siret": str(row.get("siret") or ""),
    }
    hint: Category | None = None
    if DATACENTER_NAME.search(name):
        hint = Category.DATACENTER
    else:
        quantity = (rubriques.get("2925") or {}).get("quantiteTotale")
        try:
            kw = float(quantity) if quantity else 0.0
        except ValueError:
            kw = 0.0
        if str(row.get("codeNaf") or "").startswith("35") and kw >= MIN_BATTERY_KW:
            hint = Category.BESS
            payload["charge_kw"] = f"{kw:g}"
    if hint is None:
        return None
    aiot = str(row.get("codeAIOT") or "")
    return Signal(
        key=f"installation:{aiot}",
        kind=SignalKind.INSTALLATION,
        source=SourceKind.GEORISQUES,
        title=name,
        url=SITE.format(aiot=aiot),
        developer=name,
        communes=(row.get("commune") or "",),
        department=department,
        category_hint=hint,
        payload=payload,
    )


def collect_georisques(http: PoliteHttp, db: Database, departments: tuple[str, ...]) -> CollectReport:
    report = CollectReport()
    for dep in departments:
        page = 1
        while True:
            data = http.get_json(API.format(dep=dep, page=page), min_interval_s=0.5)
            for row in data.get("data") or []:
                report.seen += 1
                if signal := to_signal(row, dep):
                    with db.transaction():
                        db.upsert_signal(signal)
                    report.signals += 1
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1
    return report
