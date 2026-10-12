"""Raw downloaded files, stored once under their SHA-256 (deduplicated, immutable, provable)."""

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
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return digest

    def get(self, digest: str, suffix: str = ".pdf") -> bytes:
        return self.path_for(digest, suffix).read_bytes()
