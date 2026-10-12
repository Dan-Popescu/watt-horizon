"""One-off imports of data already on disk (e.g. the 2026-10-12 prototype run). No network access here."""

import email.utils
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from radar.domain.enums import Category, SignalKind, SourceKind
from radar.domain.ids import normalize_filing_number
from radar.domain.models import Signal
from radar.pipeline.ingest import ingest_pdf
from radar.store.blobs import BlobStore
from radar.store.db import Database
from radar.validation.lookups import JsonCache

# Prototype label names -> categories.
LEGACY_LABELS: dict[str, Category] = {
    "bess": Category.BESS,
    "bess_hybride": Category.BESS_HYBRID,
    "poste_stockage": Category.STORAGE_SUBSTATION,
    "datacenter": Category.DATACENTER,
    "poste_enr": Category.RENEWABLE_SUBSTATION,
    "poste_public": Category.PUBLIC_GRID,
    "batteries_dechets": Category.BATTERY_WASTE,
    "autre": Category.OTHER,
    "a_verifier": Category.NEEDS_REVIEW,
}


def import_files(
    db: Database,
    blobs: BlobStore,
    index_path: Path,
    docs_dir: Path,
    region: str,
    transcripts_in: Path | None,
    transcripts_out: Path,
) -> dict[str, int]:
    """Index format: [{"title", "url", "pdfs": [{"url", "file"}]}]. Transcripts are named `<file>.txt` on input
    and re-keyed to `<document_id>.txt` (what `TranscriptOcr` expects)."""
    stats = {"documents": 0, "transcripts": 0, "skipped": 0}
    articles: list[dict[str, Any]] = json.loads(index_path.read_text(encoding="utf-8"))
    with db.transaction():
        for article in articles:
            for pdf in article.get("pdfs", []):
                path = docs_dir / pdf.get("file", "")
                number = normalize_filing_number(pdf.get("file"))
                if not path.is_file() or number is None:
                    stats["skipped"] += 1
                    continue
                record = ingest_pdf(
                    db,
                    blobs,
                    data=path.read_bytes(),
                    source=SourceKind.DREAL_CASE_BY_CASE,
                    region=region,
                    number=number,
                    url=pdf["url"],
                    title=article["title"],
                    filename=pdf["file"],
                )
                stats["documents"] += 1
                transcript = transcripts_in / f"{pdf['file']}.txt" if transcripts_in else None
                if transcript and transcript.is_file():
                    transcripts_out.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(transcript, transcripts_out / f"{record.id}.txt")
                    stats["transcripts"] += 1
    return stats


def import_registry_signals(db: Database, path: Path) -> int:
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    with db.transaction():
        for r in rows:
            db.upsert_signal(
                Signal(
                    key=f"company:{r['siren']}",
                    kind=SignalKind.COMPANY,
                    source=SourceKind.COMPANY_REGISTRY,
                    title=r["nom"],
                    url=r["url"],
                    observed_on=date.fromisoformat(r["creation"]) if r.get("creation") else None,
                    developer=r["nom"],
                    department=r.get("dep"),
                    category_hint=Category.DATACENTER if r.get("type") == "datacenter" else Category.BESS,
                    payload={
                        "naf": r.get("naf") or "",
                        "siege": r.get("commune_siege") or "",
                        "parents": "; ".join(p for p in r.get("dirigeants_personnes_morales") or [] if p),
                    },
                )
            )
    return len(rows)


def import_press_signals(db: Database, path: Path) -> int:
    """Only articles citing a commune of the target area become signals (same rule as the live connector)."""
    count = 0
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    with db.transaction():
        for r in rows:
            if not r.get("communes_citees"):
                continue
            published = email.utils.parsedate_to_datetime(r["date"]).date() if r.get("date") else None
            text = f"{r['titre']} {r.get('resume', '')}".lower()
            db.upsert_signal(
                Signal(
                    key="article:" + hashlib.sha1(r["lien"].encode("utf-8")).hexdigest()[:16],
                    kind=SignalKind.ARTICLE,
                    source=SourceKind.PRESS,
                    title=r["titre"],
                    url=r["lien"],
                    observed_on=published,
                    communes=tuple(r["communes_citees"]),
                    category_hint=Category.DATACENTER if "data" in text or "centre de donn" in text else Category.BESS,
                    payload={"feed": r.get("source", ""), "summary": (r.get("resume") or "")[:300]},
                )
            )
            count += 1
    return count


def import_gold(source: Path, target: Path) -> int:
    """Prototype gold file ({"dossier", "label", "note", "annotateur"}) -> project format ({"number", "category"...})."""
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        number = normalize_filing_number(row.get("number") or row.get("dossier"))
        category = LEGACY_LABELS.get(row.get("label", "")) or Category(row.get("category", "other"))
        lines.append(
            json.dumps(
                {
                    "number": number,
                    "category": category.value,
                    "note": row.get("note", ""),
                    "annotator": row.get("annotateur") or row.get("annotator", ""),
                },
                ensure_ascii=False,
            )
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def import_cache(source: Path, cache: JsonCache) -> int:
    return cache.merge(json.loads(source.read_text(encoding="utf-8")))


def import_communes(source: Path, target: Path) -> int:
    rows = json.loads(source.read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return len(rows)
