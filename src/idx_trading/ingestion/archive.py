"""Layer 1 raw archive: immutable, content-addressed files (PRD §7.1)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from idx_trading.ingestion.schemas import RawPayload

_EXT = {
    "text/csv": "csv",
    "application/json": "json",
    "text/html": "html",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size_bytes: int
    relative_path: str
    newly_written: bool


class RawArchive:
    """Files are stored at `<root>/<source_id>/<sha[:2]>/<sha>.<ext>`.

    Writing the same content twice is a no-op, so reruns never duplicate data.
    A JSON sidecar keeps the retrieval metadata next to the bytes.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def store(self, source_id: str, payload: RawPayload) -> StoredObject:
        sha = self.digest(payload.content)
        ext = _EXT.get(payload.content_type, "bin")
        rel = Path(source_id) / sha[:2] / f"{sha}.{ext}"
        target = self.root / rel
        if target.exists():
            if self.digest(target.read_bytes()) != sha:
                raise OSError(f"archive corruption detected at {target}")
            return StoredObject(sha, len(payload.content), rel.as_posix(), False)

        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, payload.content)
        sidecar = {
            "sha256": sha,
            "source_id": source_id,
            "origin": payload.origin,
            "content_type": payload.content_type,
            "retrieved_at": payload.retrieved_at.isoformat(),
            "metadata": payload.metadata,
        }
        self._atomic_write(
            target.with_suffix(target.suffix + ".meta.json"),
            json.dumps(sidecar, indent=2, default=str).encode("utf-8"),
        )
        return StoredObject(sha, len(payload.content), rel.as_posix(), True)

    def read(self, relative_path: str) -> bytes:
        return (self.root / relative_path).read_bytes()

    def verify(self, relative_path: str, sha256: str) -> bool:
        path = self.root / relative_path
        return path.exists() and self.digest(path.read_bytes()) == sha256

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
