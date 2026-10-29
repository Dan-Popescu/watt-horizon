"""Durable receipts connecting verified campaign consensus to append-only corrections."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

REVIEW_MIGRATION = """
CREATE TABLE campaign_receipts (
    campaign TEXT NOT NULL, annotation TEXT NOT NULL, created_at TEXT NOT NULL,
    reviewed_seconds REAL, PRIMARY KEY(campaign,annotation)
);
CREATE TABLE campaign_applied_fields (
    campaign TEXT NOT NULL, annotation TEXT NOT NULL, filing_key TEXT NOT NULL,
    field TEXT NOT NULL, value TEXT NOT NULL, correction_id INTEGER NOT NULL REFERENCES corrections(id),
    evidence TEXT NOT NULL,
    PRIMARY KEY(campaign,annotation,filing_key,field),
    FOREIGN KEY(campaign,annotation) REFERENCES campaign_receipts(campaign,annotation)
);
CREATE INDEX campaign_applied_filing ON campaign_applied_fields(filing_key,field);
"""


class ReviewHistory:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def contains(self, campaign: str, annotation: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM campaign_receipts WHERE campaign=? AND annotation=?", (campaign, annotation)
            ).fetchone()
            is not None
        )

    def receipt(self, campaign: str, annotation: str, seconds: float | None) -> None:
        self.connection.execute(
            "INSERT INTO campaign_receipts VALUES (?,?,?,?)",
            (campaign, annotation, datetime.now(UTC).isoformat(), seconds),
        )

    def applied(
        self,
        campaign: str,
        annotation: str,
        key: str,
        field: str,
        value: object,
        correction_id: int,
        evidence: list[dict[str, Any]],
    ) -> None:
        self.connection.execute(
            "INSERT INTO campaign_applied_fields VALUES (?,?,?,?,?,?,?)",
            (campaign, annotation, key, field, json.dumps(value), correction_id, json.dumps(evidence)),
        )

    def summary(self) -> dict[str, Any]:
        receipts, measured, seconds = self.connection.execute(
            "SELECT COUNT(*),COUNT(reviewed_seconds),SUM(reviewed_seconds) FROM campaign_receipts"
        ).fetchone()
        fields, filings = self.connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT filing_key) FROM campaign_applied_fields"
        ).fetchone()
        return {
            "receipts": receipts,
            "applied_field_observations": fields,
            "filings": filings,
            "declared_review_seconds": seconds,
            "receipts_with_declared_time": measured,
        }
