"""Private, append-only review sessions; a server identity can see only its own blind slot."""

import hashlib
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from radar.evaluation.campaign import ANNOTATED_FIELDS, Annotation, CampaignRecord, Observation, load_campaign
from radar.evaluation.campaign_review import validate
from radar.reading.pdf import PdfContent
from radar.store.blobs import BlobStore


class ReviewConflictError(ValueError):
    """A stale browser or a completed review cannot replace a newer approved observation."""


def _atomic_text(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".review-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ReviewSession:
    def __init__(self, directory: Path, reviewer: str, slot: Literal["a", "b"]) -> None:
        reviewer = unicodedata.normalize("NFKC", reviewer).strip()
        if not reviewer or len(reviewer) > 100 or any(ord(char) < 32 for char in reviewer):
            raise ValueError("Un identifiant de lecteur privé est requis (100 caractères maximum).")
        if slot not in {"a", "b"}:
            raise ValueError("Le rôle de relecture doit être a ou b.")
        self.directory, self.reviewer, self.slot = directory, reviewer, slot
        self.campaign = load_campaign(directory)
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", document.id)
            for record in self.campaign.records
            for document in record.documents
        ):
            raise ValueError("Empreinte de document invalide dans le manifeste.")
        self.records = {
            record.filing_key: record for record in self.campaign.records if slot == "a" or record.double_review
        }
        self.reviewer_key = hashlib.sha256(reviewer.casefold().encode()).hexdigest()
        self.reviews_dir = directory / "reviews"
        self.reviews_dir.mkdir(mode=0o700, exist_ok=True)
        self.journal = self.reviews_dir / "journal.sqlite"
        self.export_path = self.reviews_dir / f"{slot}-{self.reviewer_key[:24]}.jsonl"
        with self.connection() as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS identities(slot TEXT PRIMARY KEY, reviewer_key TEXT UNIQUE, reviewer TEXT);"
                "CREATE TABLE IF NOT EXISTS revisions("
                "reviewer_key TEXT, filing_key TEXT, revision INTEGER, data TEXT, finalized INTEGER, "
                "duration_s REAL, created_at TEXT, PRIMARY KEY(reviewer_key,filing_key,revision));"
            )
            current = conn.execute("SELECT reviewer_key FROM identities WHERE slot=?", (slot,)).fetchone()
            if current is not None and current[0] != self.reviewer_key:
                raise ValueError("Ce rôle est déjà attribué à un autre lecteur.")
            try:
                conn.execute("INSERT OR IGNORE INTO identities VALUES (?,?,?)", (slot, self.reviewer_key, reviewer))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Les deux lectures doivent être attribuées à des personnes distinctes.") from exc
            assigned = conn.execute("SELECT reviewer_key FROM identities WHERE slot=?", (slot,)).fetchone()
            if assigned is None or assigned[0] != self.reviewer_key:
                raise ValueError("Les deux lectures doivent être attribuées à des personnes distinctes.")
        self._project_export()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.journal, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _check_manifest(self) -> None:
        if load_campaign(self.directory).fingerprint != self.campaign.fingerprint:
            raise ReviewConflictError("Le manifeste de campagne a changé ; rechargez une campagne valide.")

    def record(self, key: str) -> CampaignRecord:
        self._check_manifest()
        if key not in self.records:
            raise KeyError("Dossier non attribué à ce rôle de relecture.")
        return self.records[key]

    def blank(self, record: CampaignRecord) -> Annotation:
        return Annotation(
            campaign_fingerprint=self.campaign.fingerprint,
            filing_key=record.filing_key,
            reviewer=self.reviewer,
            fields={name: Observation() for name in ANNOTATED_FIELDS},
            document_relevance={document.id: Observation() for document in record.documents},
        )

    def _current(self, conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            conn.execute(
                "SELECT * FROM revisions WHERE reviewer_key=? AND filing_key=? ORDER BY revision DESC LIMIT 1",
                (self.reviewer_key, key),
            ).fetchone(),
        )

    def load(self, key: str) -> dict[str, Any]:
        record = self.record(key)
        with self.connection() as conn:
            current = self._current(conn, key)
        annotation = Annotation.model_validate_json(current["data"]) if current else self.blank(record)
        return {
            # The server binds the author. Browser payloads never disclose reviewer identities.
            "annotation": annotation.model_copy(update={"reviewer": ""}).model_dump(mode="json"),
            "revision": current["revision"] if current else 0,
            "finalized": bool(current and current["finalized"]),
            "documents": [{"id": d.id, "role": d.role, "pages": d.pages, "url": d.url} for d in record.documents],
        }

    def queue(self) -> dict[str, Any]:
        self._check_manifest()
        self._project_export()
        with self.connection() as conn:
            items = []
            for key in self.records:
                current = self._current(conn, key)
                items.append(
                    {
                        "filing_key": key,
                        "state": "finalized"
                        if current and current["finalized"]
                        else "draft"
                        if current
                        else "unreviewed",
                    }
                )
        return {"slot": self.slot, "records": items, "export": str(self.export_path.relative_to(self.directory))}

    def _project_export(self) -> None:
        # The journal is authoritative. Serialize the projection after the durable append;
        # a failed file write is repaired on the next save without losing that append.
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = []
            for record in self.records.values():
                current = self._current(conn, record.filing_key)
                encoded_row = current["data"] if current else self.blank(record).model_dump_json()
                rows.append(encoded_row)
                if current and current["finalized"]:
                    approved = encoded_row + "\n"
                    digest = hashlib.sha256(approved.encode()).hexdigest()
                    archive = self.reviews_dir / f"{self.slot}-{self.reviewer_key[:24]}-{digest}.jsonl"
                    if not archive.exists():
                        _atomic_text(archive, approved)
                    elif archive.read_text(encoding="utf-8") != approved:
                        raise ReviewConflictError("Une archive de relecture a été modifiée.")
            encoded = "".join(row + "\n" for row in rows)
            if not self.export_path.exists() or self.export_path.read_text(encoding="utf-8") != encoded:
                _atomic_text(self.export_path, encoded)

    def _assert_write(self, conn: sqlite3.Connection, key: str, expected_revision: int) -> None:
        current = self._current(conn, key)
        if (current["revision"] if current else 0) != expected_revision:
            raise ReviewConflictError("Une version plus récente existe ; rechargez avant d'enregistrer.")
        if current and current["finalized"]:
            raise ReviewConflictError(
                "Cette fiche est déjà validée et reste immuable. Créez une campagne d'arbitrage pour la corriger."
            )

    def save(
        self,
        key: str,
        annotation: Annotation,
        expected_revision: int,
        finalize: bool,
        duration_s: float,
        blobs: BlobStore,
        read: Callable[[str, bytes], PdfContent],
    ) -> dict[str, Any]:
        record = self.record(key)
        if expected_revision < 0 or not math.isfinite(duration_s) or not 0 <= duration_s <= 14_400:
            raise ValueError("Révision ou durée de relecture invalide.")
        with self.connection() as conn:
            self._assert_write(conn, key, expected_revision)
        if annotation.filing_key != key or annotation.campaign_fingerprint != self.campaign.fingerprint:
            raise ValueError("L'observation ne correspond pas à ce dossier de campagne.")
        if annotation.reviewer not in {"", self.reviewer} or not annotation.blind:
            raise ValueError("L'identité du lecteur et le mode aveugle sont fixés par la session.")
        ids = {document.id for document in record.documents}
        if set(annotation.fields) != set(ANNOTATED_FIELDS) or set(annotation.document_relevance) != ids:
            raise ValueError("Les champs et pièces doivent correspondre au manifeste.")
        if not set(annotation.reviewed_documents) <= ids or len(set(annotation.reviewed_documents)) != len(
            annotation.reviewed_documents
        ):
            raise ValueError("Liste de pièces examinées invalide.")
        annotation = annotation.model_copy(update={"reviewer": self.reviewer, "verified": finalize, "blind": True})
        if finalize:
            observations = [*annotation.fields.values(), *annotation.document_relevance.values()]
            if set(annotation.reviewed_documents) != ids or any(o.status == "unreviewed" for o in observations):
                raise ValueError(
                    "Examinez chaque pièce et chaque champ avant validation ; utilisez « inconnu » si nécessaire."
                )
            descriptor, name = tempfile.mkstemp(prefix=".validate-", suffix=".jsonl", dir=self.reviews_dir)
            path = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(annotation.model_dump_json() + "\n")
                validate(self.campaign, [path], blobs, read=read)
            finally:
                path.unlink(missing_ok=True)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_write(conn, key, expected_revision)
            conn.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                (
                    self.reviewer_key,
                    key,
                    expected_revision + 1,
                    annotation.model_dump_json(),
                    int(finalize),
                    duration_s,
                    datetime.now(UTC).isoformat(),
                ),
            )
        self._project_export()
        return self.load(key)
