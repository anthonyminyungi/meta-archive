"""Media verification, streaming MP4 inspection, DASH muxing, and XMP sidecar utilities."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from PIL import Image

from meta_archive_pipeline.utils.fs import atomic_write_bytes, to_long_path

_IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png", ".tif", ".tiff", ".heic", ".avif"}
_CONTAINER_BOXES = {b"moov", b"trak", b"mdia"}
_EXIF_DT_RE = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[T ](\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:?\d{2})?$"
)
_TZ_OFFSET_RE = re.compile(r"^(Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class ImageCheckResult:
    """Result of validating an image file with Pillow."""

    ok: bool
    width: int | None = None
    height: int | None = None
    format: str | None = None
    has_exif_datetime: bool = False
    error: str | None = None


@dataclass(frozen=True)
class Mp4CheckResult:
    """Result of streaming ISO BMFF MP4 container and track inspection."""

    ok: bool
    has_video: bool = False
    has_audio: bool = False
    error: str | None = None


def _has_exif_datetime(exif: Image.Exif | None) -> bool:
    if not exif:
        return False
    if 36867 in exif or 306 in exif:
        return True
    if 34665 in exif:
        try:
            exif_ifd = exif.get_ifd(34665)
            return 36867 in exif_ifd
        except Exception:
            return False
    return False


def verify_image(path: Path) -> ImageCheckResult:
    """Verify image integrity without relying solely on file extension."""
    long_path = to_long_path(path)
    try:
        if os.path.getsize(long_path) == 0:
            return ImageCheckResult(ok=False, error="0-byte file")
    except OSError as exc:
        return ImageCheckResult(ok=False, error=str(exc))

    try:
        with open(long_path, "rb") as handle:
            with Image.open(handle) as img:
                img.verify()
        with open(long_path, "rb") as handle:
            with Image.open(handle) as img:
                width, height = img.size
                fmt = img.format
                has_exif_dt = _has_exif_datetime(img.getexif())
        return ImageCheckResult(
            ok=True,
            width=width,
            height=height,
            format=fmt,
            has_exif_datetime=has_exif_dt,
        )
    except Exception as exc:
        return ImageCheckResult(ok=False, error=str(exc))


def _scan_mp4_boxes(
    handle: Any,
    start: int,
    end: int,
    state: dict[str, bool],
) -> None:
    pos = start
    while pos < end:
        if end - pos < 8:
            raise ValueError("Truncated MP4 box header")
        handle.seek(pos)
        header = handle.read(8)
        size32, box_type = struct.unpack(">I4s", header)
        header_len = 8

        if size32 == 1:
            if end - pos < 16:
                raise ValueError("Truncated MP4 extended box size")
            box_size = struct.unpack(">Q", handle.read(8))[0]
            header_len = 16
        elif size32 == 0:
            box_size = end - pos
        else:
            box_size = size32

        if box_size < header_len or pos + box_size > end:
            raise ValueError(f"Malformed MP4 box size {box_size} for {box_type!r}")

        if box_type == b"ftyp" and pos == 0:
            state["has_ftyp"] = True
        elif box_type == b"moov":
            state["has_moov"] = True
            _scan_mp4_boxes(handle, pos + header_len, pos + box_size, state)
        elif box_type in _CONTAINER_BOXES:
            _scan_mp4_boxes(handle, pos + header_len, pos + box_size, state)
        elif box_type == b"hdlr":
            payload_start = pos + header_len
            payload_len = box_size - header_len
            if payload_len >= 12:
                handle.seek(payload_start + 8)
                handler_type = handle.read(4)
                if handler_type == b"vide":
                    state["has_video"] = True
                elif handler_type == b"soun":
                    state["has_audio"] = True

        pos += box_size
        handle.seek(pos)


def inspect_mp4(path: Path) -> Mp4CheckResult:
    """Inspect MP4 container headers and track types via streaming box seeks."""
    long_path = to_long_path(path)
    try:
        file_size = os.path.getsize(long_path)
    except OSError as exc:
        return Mp4CheckResult(ok=False, error=str(exc))

    if file_size == 0:
        return Mp4CheckResult(ok=False, error="0-byte file")
    if file_size < 16:
        return Mp4CheckResult(ok=False, error="File too small to be valid MP4")

    state = {
        "has_ftyp": False,
        "has_moov": False,
        "has_video": False,
        "has_audio": False,
    }
    try:
        with open(long_path, "rb") as handle:
            _scan_mp4_boxes(handle, 0, file_size, state)
    except Exception as exc:
        return Mp4CheckResult(ok=False, error=str(exc))

    if not state["has_ftyp"]:
        return Mp4CheckResult(ok=False, error="Missing ftyp box")
    if not state["has_moov"]:
        return Mp4CheckResult(ok=False, error="Missing moov box")

    return Mp4CheckResult(
        ok=True,
        has_video=state["has_video"],
        has_audio=state["has_audio"],
    )


def resolve_ffmpeg() -> str | None:
    """Locate system ffmpeg on PATH or fall back to bundled imageio-ffmpeg."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            return bundled
    except Exception:
        return None
    return None


def mux_dash_streams(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    ffmpeg_bin: str | None = None,
) -> Path:
    """Losslessly mux separate DASH video and audio tracks into an MP4 with audio verified."""
    exe = ffmpeg_bin or resolve_ffmpeg()
    if not exe:
        raise RuntimeError("No ffmpeg executable available on PATH or via imageio-ffmpeg")

    out_parent_long = to_long_path(output_path.parent)
    os.makedirs(out_parent_long, exist_ok=True)
    out_long = to_long_path(output_path)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=out_parent_long,
            prefix=f".{output_path.stem}.",
            suffix=".mp4",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name

        proc = subprocess.run(
            [
                exe,
                "-y",
                "-i",
                to_long_path(video_path),
                "-i",
                to_long_path(audio_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed ({proc.returncode}): {proc.stderr.strip()}")

        check = inspect_mp4(Path(tmp_path))
        if not check.ok or not check.has_audio:
            raise RuntimeError(f"Muxed MP4 failed verification: {check}")

        os.replace(tmp_path, out_long)
        tmp_path = None
        return output_path
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def xmp_sidecar_path(media_path: Path) -> Path:
    """Return Immich-preferred sidecar path `<filename>.<ext>.xmp`."""
    return Path(f"{media_path}.xmp")


def _parse_tz_offset(tz_offset: str) -> tuple[timezone, str]:
    if not _TZ_OFFSET_RE.match(tz_offset):
        raise ValueError(f"Invalid timezone offset format: {tz_offset!r}")
    if tz_offset == "Z":
        return timezone.utc, "+00:00"
    sign = 1 if tz_offset[0] == "+" else -1
    hours = int(tz_offset[1:3])
    minutes = int(tz_offset[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes)), tz_offset


def format_xmp_datetime(
    timestamp_unix: int | float | None = None,
    exif_datetime_original: str | None = None,
    tz_offset: str = "+09:00",
) -> str | None:
    """Format capture/post timestamp for XMP with explicit timezone offset.

    Prefers `exif_datetime_original` (from Meta JSON) over `timestamp_unix`.
    """
    tz_info, norm_offset = _parse_tz_offset(tz_offset)

    if exif_datetime_original:
        match = _EXIF_DT_RE.match(exif_datetime_original.strip())
        if match:
            year, month, day, hour, minute, second, raw_tz = match.groups()
            if raw_tz is None:
                offset = norm_offset
            elif raw_tz == "Z":
                offset = "+00:00"
            elif ":" not in raw_tz:
                offset = f"{raw_tz[:3]}:{raw_tz[3:]}"
            else:
                offset = raw_tz
            return f"{year}-{month}-{day}T{hour}:{minute}:{second}{offset}"

    if timestamp_unix is not None:
        dt = datetime.fromtimestamp(float(timestamp_unix), tz=tz_info)
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}{norm_offset}"

    return None


def format_xmp_gps(value: float, is_lat: bool) -> str:
    """Format decimal latitude/longitude into XMP EXIF GPS `DD,MM.MMMMMM[N|S|E|W]`."""
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    abs_val = abs(value)
    degrees = int(abs_val)
    minutes = (abs_val - degrees) * 60.0
    return f"{degrees},{minutes:.6f}{hemi}"


def write_xmp_sidecar(
    media_path: Path,
    *,
    timestamp_unix: int | float | None = None,
    exif_datetime_original: str | None = None,
    tz_offset: str = "+09:00",
    gps_lat: float | None = None,
    gps_lng: float | None = None,
    description: str | None = None,
    overwrite: bool = False,
) -> Path | None:
    """Write a lossless `<filename>.<ext>.xmp` sidecar without modifying original media bytes."""
    sidecar = xmp_sidecar_path(media_path)
    if not overwrite and os.path.exists(to_long_path(sidecar)):
        return sidecar

    omit_date = False
    media_long = to_long_path(media_path)
    if os.path.exists(media_long) and media_path.suffix.lower() in _IMAGE_EXTS:
        img_check = verify_image(media_path)
        if img_check.ok and img_check.has_exif_datetime:
            omit_date = True

    xmp_dt = (
        None
        if omit_date
        else format_xmp_datetime(
            timestamp_unix=timestamp_unix,
            exif_datetime_original=exif_datetime_original,
            tz_offset=tz_offset,
        )
    )

    lines: list[str] = []
    if xmp_dt:
        lines.append(f"      <exif:DateTimeOriginal>{xmp_dt}</exif:DateTimeOriginal>")
        lines.append(f"      <xmp:CreateDate>{xmp_dt}</xmp:CreateDate>")
        lines.append(f"      <photoshop:DateCreated>{xmp_dt}</photoshop:DateCreated>")
    if gps_lat is not None:
        lines.append(f"      <exif:GPSLatitude>{format_xmp_gps(gps_lat, True)}</exif:GPSLatitude>")
    if gps_lng is not None:
        lines.append(f"      <exif:GPSLongitude>{format_xmp_gps(gps_lng, False)}</exif:GPSLongitude>")
    if description:
        escaped = xml_escape(description)
        lines.extend(
            [
                "      <dc:description>",
                "        <rdf:Alt>",
                f'          <rdf:li xml:lang="x-default">{escaped}</rdf:li>',
                "        </rdf:Alt>",
                "      </dc:description>",
            ]
        )

    if not lines:
        return None

    xml_doc = "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
            '  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
            '    <rdf:Description rdf:about=""',
            '        xmlns:exif="http://ns.adobe.com/exif/1.0/"',
            '        xmlns:xmp="http://ns.adobe.com/xap/1.0/"',
            '        xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"',
            '        xmlns:dc="http://purl.org/dc/elements/1.1/">',
            *lines,
            "    </rdf:Description>",
            "  </rdf:RDF>",
            "</x:xmpmeta>",
            "",
        ]
    )
    atomic_write_bytes(sidecar, xml_doc.encode("utf-8"))
    return sidecar


def set_file_mtime(path: Path, timestamp_unix: int | float) -> None:
    """Update file atime and mtime to match a Unix timestamp."""
    ts = float(timestamp_unix)
    os.utime(to_long_path(path), (ts, ts))


def download_media_via_context(
    request_context: Any,
    url: str,
    dest_path: Path,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download media bytes using a Playwright APIRequestContext and write atomically."""
    response = (
        request_context.get(url, headers=headers)
        if headers is not None
        else request_context.get(url)
    )
    status = getattr(response, "status", None)
    ok = getattr(response, "ok", None)
    if ok is False or (isinstance(status, int) and not (200 <= status < 300)):
        raise RuntimeError(f"Failed to download {url}: HTTP {status}")

    payload = response.body()
    if not payload:
        raise RuntimeError(f"Downloaded empty response body from {url}")

    atomic_write_bytes(dest_path, payload)
    return dest_path
