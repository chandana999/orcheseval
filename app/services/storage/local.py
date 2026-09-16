"""Local filesystem storage, ported from evalforge-local.

Used only to archive an uploaded file for provenance and to write exports.
Nothing in the evaluation path ever reads from here: after ingestion the
payloads live in PostgreSQL and the file can be deleted or moved.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.core.config import settings


class LocalStorageBackend:
    """Filesystem storage. Logical paths look like local://<key>."""

    scheme = "local://"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or settings.local_storage_root).resolve()
        (self.root / "uploads").mkdir(parents=True, exist_ok=True)
        (self.root / "exports").mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        relative = key.replace("\\", "/").lstrip("/")
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("storage path escapes LOCAL_STORAGE_ROOT") from exc
        return path

    def parse_path(self, storage_path: str) -> str:
        if storage_path.startswith(self.scheme):
            return storage_path[len(self.scheme) :]
        return storage_path

    def logical_path(self, key: str) -> str:
        return f"{self.scheme}{key.replace(chr(92), '/')}"

    def upload_bytes(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
        except Exception:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return self.logical_path(key)

    def upload_jsonl(self, key: str, records: list[dict[str, Any]]) -> str:
        body = ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode(
            "utf-8"
        )
        return self.upload_bytes(key, body)

    def download_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def writable(self) -> bool:
        probe = self.root / ".write-probe"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False


storage_service = LocalStorageBackend()
