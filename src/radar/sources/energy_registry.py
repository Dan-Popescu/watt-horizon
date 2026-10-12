"""National register of electricity production and storage installations (ODRÉ, Licence Ouverte), storage rows.

It lists what is IN SERVICE, about a year late: a match tells that a tracked project was commissioned.
Rows as returned by the ODRÉ API (`registre-national-installation-production-stockage-electricite-agrege-*`).
"""

from collections.abc import Iterable
from datetime import date
from typing import Any

from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.models import Signal

DATASET_URL = "https://odre.opendatasoft.com/explore/dataset/registre-national-installation-production-stockage-electricite-agrege-311225/"


def _department(insee: str) -> str:
    return insee[:3] if insee.startswith("97") else insee[:2]


def to_signal(row: dict[str, Any]) -> Signal | None:
    insee, power_kw = row.get("codeinseecommune"), row.get("puismaxinstallee")
    if not insee or not power_kw:
        return None
    commissioned = row.get("datemiseenservice_date")
    mw = round(float(power_kw) / 1000, 3)
    energy = row.get("energiestockable")
    payload = {"insee": str(insee), "power_mw": str(mw), "installations": str(row.get("nbinstallations") or 1)}
    if energy:
        payload["energy_mwh"] = str(round(float(energy) / 1000, 3))
    if row.get("postesource"):
        payload["substation"] = str(row["postesource"])
    name = row.get("nominstallation")
    return Signal(
        key=f"registry:{insee}:{commissioned or 'na'}:{mw}",
        kind=SignalKind.INSTALLATION,
        source=SourceKind.ENERGY_REGISTRY,
        title=f"Stockage en service — {row.get('commune') or insee} ({mw:g} MW)",
        url=DATASET_URL,
        observed_on=date.fromisoformat(commissioned) if commissioned else None,
        developer=name if name and name != "Confidentiel" else None,
        communes=(row["commune"],) if row.get("commune") else (),
        department=_department(str(insee)),
        category_hint=Category.BESS,
        payload=payload,
    )


def to_signals(rows: Iterable[dict[str, Any]]) -> list[Signal]:
    return [s for row in rows if (s := to_signal(row))]
