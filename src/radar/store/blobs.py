"""Raw downloaded files, stored once under their SHA-256 (deduplicated, immutable, provable)."""

import os
import tempfile
from pathlib import Path

from radar.domain.ids import sha256


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, digest: str, suffix: str = ".pdf") -> Path:
        return self.root / digest[:2] / f"{digest}{suffix}"

    def put(self, data: bytes, suffix: str = ".pdf") -> str:
        digest = sha256(data)
        path = self.path_for(digest, suffix)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Unique staging names keep concurrent writers from replacing or deleting
            # each other's partial files. Only complete, synced bytes become visible.
            tmp: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=f".{digest}.", suffix=".tmp", delete=False
                ) as file:
                    tmp = Path(file.name)
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                tmp.replace(path)
                if hasattr(os, "O_DIRECTORY"):
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
        return digest

    def get(self, digest: str, suffix: str = ".pdf", *, max_bytes: int | None = None) -> bytes:
        path = self.path_for(digest, suffix)
        if max_bytes is None:
            return path.read_bytes()
        if max_bytes < 0:
            raise ValueError("Blob byte limit cannot be negative")
        if path.stat().st_size > max_bytes:
            raise ValueError(f"Blob exceeds byte limit ({max_bytes})")
        # A file may grow between stat and open; never let that allocate unbounded RAM.
        with path.open("rb") as file:
            data = file.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"Blob exceeds byte limit ({max_bytes})")
        return data
