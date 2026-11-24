"""Filesystem helpers: Windows long-path support, streaming SHA-256, and atomic writes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def to_long_path(path: Path | str) -> str:
    """Return an OS path string safe for paths exceeding MAX_PATH on Windows."""
    raw = os.fspath(path)
    if os.name != "nt":
        return raw

    if raw.startswith("\\\\?\\"):
        return raw

    abs_path = os.path.abspath(raw)
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute hex SHA-256 digest of a file using streaming reads."""
    digest = hashlib.sha256()
    with open(to_long_path(path), "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically via same-directory temp file, fsync, and os.replace."""
    parent_long = to_long_path(path.parent)
    os.makedirs(parent_long, exist_ok=True)
    target_long = to_long_path(path)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent_long,
            prefix=f".{path.name[:80]}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target_long)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
