"""Canonical archive directory layout and cross-platform relative path helpers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ARCHIVE_SCHEMA_VERSION = 1

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def normalize_rel_path(path: str | Path) -> str:
    """Normalize a relative archive path to POSIX `/` separators and Unicode NFC.

    Rejects empty paths, parent directory traversal (`..`), and Windows drive prefixes.
    """
    raw = str(path).replace("\\", "/")
    if _DRIVE_PREFIX_RE.match(raw):
        raise ValueError(f"Drive-letter paths are not allowed in archive relative paths: {path!r}")

    stripped = raw.lstrip("/")
    parts: list[str] = []
    for part in stripped.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"Parent traversal ('..') is not allowed in archive relative paths: {path!r}")
        parts.append(unicodedata.normalize("NFC", part))

    if not parts:
        raise ValueError(f"Archive relative path cannot be empty: {path!r}")

    return "/".join(parts)


@dataclass(frozen=True)
class ArchivePaths:
    """Standard file-system layout inside a built Meta Archive directory."""

    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def db_path(self) -> Path:
        return self.root / "archive.sqlite"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.sha256"

    @property
    def ambiguous_matches_path(self) -> Path:
        return self.reports_dir / "ambiguous_matches.json"

    @property
    def profiles_json(self) -> Path:
        return self.data_dir / "profiles.json"

    @property
    def posts_json(self) -> Path:
        return self.data_dir / "posts.json"

    @property
    def stories_json(self) -> Path:
        return self.data_dir / "stories.json"

    @property
    def connections_json(self) -> Path:
        return self.data_dir / "connections.json"

    @property
    def messages_json(self) -> Path:
        return self.data_dir / "messages.json"

    @staticmethod
    def media_rel_path(platform: str, category: str, filename: str) -> str:
        """Build a normalized relative path under `media/<platform>/<category>/<filename>`."""
        for label, value in (("platform", platform), ("category", category)):
            cleaned = normalize_rel_path(value)
            if "/" in cleaned:
                raise ValueError(f"{label} must be a single path segment: {value!r}")
        return normalize_rel_path(f"media/{platform}/{category}/{filename}")
