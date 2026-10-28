"""Weekly alert e-mail: new filings since a date + change log. Built from `api/queries.py`, rendered as
e-mail-safe HTML (tables, inline styles) and plain text. Labels are French (client-facing)."""

from dataclasses import dataclass
from datetime import date
from html import escape

from radar.api import queries
from radar.api.views import ChangeView, ProjectDetail
from radar.store.db import Database

CATEGORY_LABELS = {
    "bess": "Batteries",
    "bess_hybrid": "Batteries + solaire/éolien",
    "storage_substation": "Poste pour batteries",
    "datacenter": "Data center",
}
CATEGORY_COLORS = {
    "bess": "#e5484d",
    "bess_hybrid": "#f76b15",
    "storage_substation": "#0e9f8e",
    "datacenter": "#8b5cf6",
}
CONFIDENCE_LABELS = {"high": "fiable", "medium": "assez fiable", "review": "à vérifier"}
ROLE_PRIORITY = {"decision": 0, "form": 1}


@dataclass(frozen=True)
class Digest:
    since: str
    generated_on: date
    scope: str  # e.g. "Hauts-de-France" or "département 60"
    new_projects: list[ProjectDetail]
    changes: list[ChangeView]  # change-log entries not already listed as new filings
    map_url: str | None = None

    @property
    def declared_mw(self) -> float:
        return sum(p.summary.power_mw or 0 for p in self.new_projects if p.summary.power_status == "known")


def build_digest(
    db: Database,
    since: str,
    *,
    department: str | None = None,
    category: str | None = None,
    scope: str = "zone suivie",
    map_url: str | None = None,
    today: date | None = None,
) -> Digest:
    summaries = queries.search_projects(
        db, category=category, department=department, since=since, limit=queries.MAX_LIMIT
    )
    details = [d for s in summaries if (d := queries.get_project(db, s.project_id)) is not None]
    details.sort(key=lambda d: d.summary.first_seen or "", reverse=True)
    listed = {d.summary.project_id for d in details}
    changes = [
        c
        for c in queries.what_changed(db, since, category=category, department=department, limit=queries.MAX_LIMIT)
        if not (c.kind == "new" and c.project_id in listed)
    ]
    return Digest(since, today or date.today(), scope, details, changes, map_url)


# --- Rendering -------------------------------------------------------------------------------------


def _fr_date(iso: str | None) -> str:
    if not iso:
        return "date inconnue"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}/{y}"


def _num(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") if value >= 100 else f"{value:g}".replace(".", ",")


def _power(p: ProjectDetail) -> str:
    s = p.summary
    if s.power_mw is None:
        return "puissance non indiquée"
    approx = "≈ " if s.power_status == "estimated" else ""
    low = f"{_num(s.power_mw_min)}–" if s.power_mw_min is not None and s.power_mw_min != s.power_mw else ""
    energy = f" / {_num(s.energy_mwh)} MWh" if s.energy_mwh else ""
    return f"{approx}{low}{_num(s.power_mw)} MW{energy}"


def _source_url(p: ProjectDetail) -> str | None:
    docs = sorted(p.documents, key=lambda d: ROLE_PRIORITY.get(d.role, 9))
    return next((d.url for d in docs if d.url.startswith(("https://", "http://"))), None)


def _headline(d: Digest) -> str:
    n = len(d.new_projects)
    parts = [f"{n} nouveau{'x' if n > 1 else ''} dossier{'s' if n > 1 else ''}"]
    if d.declared_mw:
        parts.append(f"{_num(d.declared_mw)} MW déclarés")
    if d.changes:
        parts.append(f"{len(d.changes)} mise{'s' if len(d.changes) > 1 else ''} à jour")
    return " · ".join(parts)


def render_text(d: Digest) -> str:
    lines = [f"WattHorizon — {d.scope} — depuis le {_fr_date(d.since)}", _headline(d), ""]
    for p in d.new_projects:
        s = p.summary
        lines += [
            f"* {s.name}",  # the name already starts with the category label
            f"  {s.developer or 'porteur inconnu'} · {_power(p)} · poste : {s.substation or 'non indiqué'}",
            f"  {s.status} · déposé le {_fr_date(s.first_seen)} · {CONFIDENCE_LABELS.get(s.confidence, s.confidence)}",
        ]
        if url := _source_url(p):
            lines.append(f"  Source : {url}")
    if d.changes:
        lines += ["", "Mises à jour :"]
        lines += [f"* {c.summary}" for c in d.changes]
    if d.map_url:
        lines += ["", f"Carte : {d.map_url}"]
    lines += ["", "Données publiques (DREAL, MRAe, registres). Chaque information renvoie à son document source."]
    return "\n".join(lines) + "\n"


_CELL = "padding:14px 16px;border-bottom:1px solid #e6e9ef;font:14px/1.5 Arial,Helvetica,sans-serif;color:#18202c"


def _project_row(p: ProjectDetail, map_url: str | None) -> str:
    s = p.summary
    color = CATEGORY_COLORS.get(s.category, "#5d6778")
    links = []
    if url := _source_url(p):
        links.append(f'<a href="{escape(url)}" style="color:#2457d6">Document source</a>')
    if map_url:
        links.append(
            f'<a href="{escape(map_url)}#p={escape(s.project_id)}" style="color:#2457d6">Voir sur la carte</a>'
        )
    review = s.confidence == "review"
    return f"""<tr><td style="{_CELL};border-left:4px solid {color}">
  <div style="font-size:12px;color:{color};font-weight:bold;text-transform:uppercase">{escape(CATEGORY_LABELS.get(s.category, s.category))}</div>
  <div style="font-size:16px;font-weight:bold">{escape(s.name)}</div>
  <div style="color:#5d6778">{escape(s.developer or "porteur inconnu")} · {escape(s.department or "")}</div>
  <div style="font-size:18px;font-weight:bold;margin-top:6px">{escape(_power(p))}</div>
  <div>Poste : <b>{escape(s.substation or "non indiqué")}</b> · Déposé le {escape(_fr_date(s.first_seen))}</div>
  <div style="color:#5d6778">{escape(s.status)}{" · <span style='color:#9a5b00'>à vérifier</span>" if review else ""}</div>
  <div style="margin-top:6px">{" · ".join(links)}</div>
</td></tr>"""


def _change_row(c: ChangeView) -> str:
    return f'<tr><td style="{_CELL}">{escape(c.summary)}</td></tr>'


def render_html(d: Digest) -> str:
    rows = "".join(_project_row(p, d.map_url) for p in d.new_projects) or (
        f'<tr><td style="{_CELL};color:#5d6778">Aucun nouveau dossier sur la période.</td></tr>'
    )
    changes = ""
    if d.changes:
        changes = f'<tr><td style="{_CELL};background:#f4f6f9;font-weight:bold">Mises à jour</td></tr>' + "".join(
            _change_row(c) for c in d.changes
        )
    cta = (
        f'<p style="margin:20px 0"><a href="{escape(d.map_url)}" style="background:#2457d6;color:#fff;padding:10px 16px;'
        f'border-radius:8px;text-decoration:none;font-weight:bold">Ouvrir la carte</a></p>'
        if d.map_url
        else ""
    )
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>WattHorizon — {escape(d.scope)}</title></head>
<body style="margin:0;background:#eef1f5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:12px;overflow:hidden">
<tr><td style="padding:22px 16px 6px;font:14px/1.5 Arial,Helvetica,sans-serif;color:#18202c">
  <div style="font-size:12px;color:#5d6778;text-transform:uppercase;letter-spacing:.05em">WattHorizon · {escape(d.scope)}</div>
  <div style="font-size:22px;font-weight:bold;margin:4px 0">Depuis le {escape(_fr_date(d.since))}</div>
  <div style="font-size:15px">{escape(_headline(d))}</div>{cta}
</td></tr>
{rows}{changes}
<tr><td style="padding:16px;font:12px/1.5 Arial,Helvetica,sans-serif;color:#5d6778">
  Données publiques (DREAL, MRAe, registres d'entreprises). Chaque information est reliée à son document source.
  « À vérifier » : extraction automatique pas encore relue. Généré le {escape(_fr_date(d.generated_on.isoformat()))}.
</td></tr>
</table></td></tr></table>
</body></html>
"""
