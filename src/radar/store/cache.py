"""Disposable key-value cache for expensive derived results (OCR pages, LLM answers).

Separate SQLite file (`data/cache/derived.sqlite`): deleting it only costs recomputation. Keys must include
everything that changes the result (document hash, engine/model, prompt version), so a new version never
reads a stale answer.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class KeyValueCache:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (namespace, key))"
        )
        self.conn.commit()
        self.hits = self.misses = 0

    def get(self, namespace: str, key: str) -> Any | None:
        row = self.conn.execute("SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key)).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(row[0])

    def put(self, namespace: str, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO kv (namespace, key, value) VALUES (?, ?, ?)",
            (namespace, key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    def items(self, namespace: str) -> list[tuple[str, Any]]:
        rows = self.conn.execute("SELECT key, value FROM kv WHERE namespace = ? ORDER BY created_at, key", (namespace,))
        return [(key, json.loads(value)) for key, value in rows]

    def delete(self, namespace: str, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
