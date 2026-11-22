"""Official Meta (Instagram & Facebook) JSON export parsers and shared helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from meta_archive_pipeline.schema.models import (
    Connection,
    MediaItem,
    MessageThread,
    Platform,
    Post,
    Profile,
    Story,
)
from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.fs import to_long_path
from meta_archive_pipeline.utils.mojibake import fix_mojibake_tree

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus"}


@dataclass
class OfficialExportBundle:
    """Normalized data extracted from a Meta official export directory."""

    platform: Platform
    root_dir: Path
    profile: Profile | None = None
    posts: list[Post] = field(default_factory=list)
    stories: list[Story] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    messages: list[MessageThread] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra_roots: list[Path] = field(default_factory=list)


def load_fixed_json(path: Path) -> Any:
    """Read a UTF-8 JSON file using Windows long-path support and fix mojibake strings."""
    with open(to_long_path(path), "rb") as handle:
        raw = json.loads(handle.read().decode("utf-8"))
    return fix_mojibake_tree(raw)


def safe_int(val: Any) -> int | None:
    """Convert int/float/digit string to int, returning None otherwise."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        stripped = val.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def safe_float(val: Any) -> float | None:
    """Convert numeric value or numeric string to float, returning None otherwise."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            return None
    return None


def ts_to_dt(ts: int | None) -> datetime | None:
    """Convert Unix seconds timestamp to UTC datetime."""
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def infer_media_type(uri: str) -> Literal["image", "video", "audio", "unknown"]:
    """Infer media_type from a relative URI or filename extension."""
    ext = Path(uri.split("?", 1)[0]).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "unknown"


def extract_exif_from_meta_metadata(
    meta: Any,
) -> tuple[str | None, float | None, float | None]:
    """Extract `(date_time_original, latitude, longitude)` from Meta `media_metadata`."""
    if not isinstance(meta, dict):
        return None, None, None

    dt_orig: str | None = None
    lat: float | None = None
    lng: float | None = None

    for key in ("photo_metadata", "video_metadata"):
        sub = meta.get(key)
        if not isinstance(sub, dict):
            continue
        exif_list = sub.get("exif_data")
        if not isinstance(exif_list, list):
            continue
        for entry in exif_list:
            if not isinstance(entry, dict):
                continue
            if not dt_orig and isinstance(entry.get("date_time_original"), str):
                cand_dt = entry["date_time_original"].strip()
                if cand_dt:
                    dt_orig = cand_dt
            if lat is None:
                cand_lat = safe_float(entry.get("latitude"))
                if cand_lat is not None and cand_lat != 0.0:
                    lat = cand_lat
            if lng is None:
                cand_lng = safe_float(entry.get("longitude"))
                if cand_lng is not None and cand_lng != 0.0:
                    lng = cand_lng

    return dt_orig, lat, lng


def normalize_uri_if_local(uri: Any) -> str | None:
    """Normalize a non-empty non-HTTP URI to POSIX/NFC relative path, else None."""
    if not isinstance(uri, str):
        return None
    stripped = uri.strip()
    if not stripped or stripped.startswith(("http://", "https://")):
        return None
    return normalize_rel_path(stripped)


def media_item_from_meta_entry(entry: dict[str, Any]) -> MediaItem:
    """Build a canonical MediaItem from an official Meta JSON media entry dict."""
    raw_uri = entry.get("uri") if isinstance(entry.get("uri"), str) else ""
    local_path = normalize_uri_if_local(raw_uri)
    stem = Path(raw_uri).stem if raw_uri else ""
    media_id = stem if stem.isdigit() else None
    dt_orig, lat, lng = extract_exif_from_meta_metadata(entry.get("media_metadata"))

    return MediaItem(
        media_id=media_id,
        media_type=infer_media_type(raw_uri),
        local_path=local_path,
        original_uri=raw_uri or None,
        taken_at_exif=dt_orig,
        gps_lat=lat,
        gps_lng=lng,
    )
