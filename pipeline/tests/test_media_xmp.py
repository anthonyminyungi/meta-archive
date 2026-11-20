"""Tests for media verification, streaming MP4 inspection, DASH muxing, and XMP sidecars."""

from __future__ import annotations

import os
import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from meta_archive_pipeline.utils.fs import sha256_file
from meta_archive_pipeline.utils.media import (
    download_media_via_context,
    format_xmp_datetime,
    format_xmp_gps,
    inspect_mp4,
    mux_dash_streams,
    resolve_ffmpeg,
    set_file_mtime,
    verify_image,
    write_xmp_sidecar,
    xmp_sidecar_path,
)


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _hdlr_box(handler_type: bytes) -> bytes:
    # FullBox version/flags (4B) + pre_defined (4B) + handler_type (4B)
    return _box(b"hdlr", b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + handler_type)


def _synthetic_mp4(*, include_audio: bool) -> bytes:
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isom")
    traks = [_box(b"trak", _box(b"mdia", _hdlr_box(b"vide")))]
    if include_audio:
        traks.append(_box(b"trak", _box(b"mdia", _hdlr_box(b"soun"))))
    moov = _box(b"moov", b"".join(traks))
    mdat = _box(b"mdat", b"\x00" * 64)
    return ftyp + moov + mdat


def test_verify_image_valid_corrupt_zerobyte_and_exif(tmp_path: Path) -> None:
    valid_jpg = tmp_path / "valid.jpg"
    Image.new("RGB", (32, 24), color=(10, 20, 30)).save(valid_jpg, format="JPEG")
    res_ok = verify_image(valid_jpg)
    assert res_ok.ok is True
    assert (res_ok.width, res_ok.height, res_ok.format) == (32, 24, "JPEG")
    assert res_ok.has_exif_datetime is False

    exif_jpg = tmp_path / "with_exif.jpg"
    img = Image.new("RGB", (16, 16), color=(255, 0, 0))
    exif = Image.Exif()
    exif[36867] = "2024:05:01 12:00:00"
    img.save(exif_jpg, format="JPEG", exif=exif)
    res_exif = verify_image(exif_jpg)
    assert res_exif.ok is True
    assert res_exif.has_exif_datetime is True

    zero_jpg = tmp_path / "zero.jpg"
    zero_jpg.write_bytes(b"")
    assert verify_image(zero_jpg).ok is False

    corrupt_jpg = tmp_path / "corrupt.jpg"
    corrupt_jpg.write_bytes(b"not a real jpeg image")
    assert verify_image(corrupt_jpg).ok is False


def test_inspect_mp4_streaming_variants(tmp_path: Path) -> None:
    av_mp4 = tmp_path / "av.mp4"
    av_mp4.write_bytes(_synthetic_mp4(include_audio=True))
    res_av = inspect_mp4(av_mp4)
    assert res_av.ok is True
    assert res_av.has_video is True
    assert res_av.has_audio is True

    v_mp4 = tmp_path / "video_only.mp4"
    v_mp4.write_bytes(_synthetic_mp4(include_audio=False))
    res_v = inspect_mp4(v_mp4)
    assert res_v.ok is True
    assert res_v.has_video is True
    assert res_v.has_audio is False

    zero_mp4 = tmp_path / "zero.mp4"
    zero_mp4.write_bytes(b"")
    assert inspect_mp4(zero_mp4).ok is False

    bad_mp4 = tmp_path / "bad.mp4"
    bad_mp4.write_bytes(b"invalid mp4 header bytes 1234567890")
    assert inspect_mp4(bad_mp4).ok is False

    # Box with size < 8 must fail cleanly rather than infinite-looping.
    loop_mp4 = tmp_path / "malformed_size.mp4"
    loop_mp4.write_bytes(_box(b"ftyp", b"isom") + struct.pack(">I4s", 4, b"moov") + b"\x00" * 16)
    assert inspect_mp4(loop_mp4).ok is False


def test_resolve_ffmpeg_and_mux_dash_streams(tmp_path: Path) -> None:
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg is not None
    assert Path(ffmpeg).exists()

    video_only = tmp_path / "dash_video.mp4"
    audio_only = tmp_path / "dash_audio.m4a"
    muxed_out = tmp_path / "muxed" / "final.mp4"

    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=32x32:d=0.2",
            "-c:v",
            "mpeg4",
            str(video_only),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            "0.2",
            "-c:a",
            "aac",
            str(audio_only),
        ],
        check=True,
        capture_output=True,
    )

    assert inspect_mp4(video_only).has_audio is False

    result = mux_dash_streams(video_only, audio_only, muxed_out, ffmpeg_bin=ffmpeg)
    assert result == muxed_out
    final_check = inspect_mp4(muxed_out)
    assert final_check.ok is True
    assert final_check.has_video is True
    assert final_check.has_audio is True


def test_xmp_sidecar_and_mtime(tmp_path: Path) -> None:
    assert format_xmp_datetime(timestamp_unix=1744201800, tz_offset="+09:00") == "2025-04-09T21:30:00+09:00"
    assert (
        format_xmp_datetime(
            timestamp_unix=1700000000,
            exif_datetime_original="2024:05:12 14:30:05",
            tz_offset="+09:00",
        )
        == "2024-05-12T14:30:05+09:00"
    )
    assert format_xmp_gps(33.4996, True) == "33,29.976000N"
    assert format_xmp_gps(-122.4194, False) == "122,25.164000W"

    # 1. Image without embedded EXIF gets sidecar without mutating original media bytes
    jpg = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 20), color=(0, 128, 255)).save(jpg, format="JPEG")
    before_sha = sha256_file(jpg)

    sidecar = write_xmp_sidecar(
        jpg,
        timestamp_unix=1744201800,
        exif_datetime_original="2024:05:12 14:30:05",
        tz_offset="+09:00",
        gps_lat=33.4996,
        gps_lng=126.5312,
        description='제주 바다 & <테스트>',
    )
    assert sidecar == xmp_sidecar_path(jpg)
    assert sidecar.name == "photo.jpg.xmp"
    assert sha256_file(jpg) == before_sha

    root = ET.fromstring(sidecar.read_text(encoding="utf-8"))
    ns = {
        "exif": "http://ns.adobe.com/exif/1.0/",
        "xmp": "http://ns.adobe.com/xap/1.0/",
        "photoshop": "http://ns.adobe.com/photoshop/1.0/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    }
    assert root.find(".//exif:DateTimeOriginal", ns).text == "2024-05-12T14:30:05+09:00"
    assert root.find(".//xmp:CreateDate", ns).text == "2024-05-12T14:30:05+09:00"
    assert root.find(".//photoshop:DateCreated", ns).text == "2024-05-12T14:30:05+09:00"
    assert root.find(".//exif:GPSLatitude", ns).text == "33,29.976000N"
    assert root.find(".//exif:GPSLongitude", ns).text == "126,31.872000E"
    assert root.find(".//dc:description//rdf:li", ns).text == "제주 바다 & <테스트>"

    # 2. MP4 gets <filename>.mp4.xmp and mtime update
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(_synthetic_mp4(include_audio=True))
    mp4_sidecar = write_xmp_sidecar(mp4, timestamp_unix=1744201800, tz_offset="+09:00")
    assert mp4_sidecar == tmp_path / "clip.mp4.xmp"
    assert "2025-04-09T21:30:00+09:00" in mp4_sidecar.read_text(encoding="utf-8")

    set_file_mtime(mp4, 1744201800)
    assert int(os.stat(mp4).st_mtime) == 1744201800

    # 3. Image with existing embedded EXIF does not get date overridden
    exif_jpg = tmp_path / "camera.jpg"
    im = Image.new("RGB", (16, 16), color=(1, 2, 3))
    exif = Image.Exif()
    exif[36867] = "2023:01:02 03:04:05"
    im.save(exif_jpg, format="JPEG", exif=exif)
    assert write_xmp_sidecar(exif_jpg, timestamp_unix=1744201800) is None
    assert not (tmp_path / "camera.jpg.xmp").exists()


class _FakeResponse:
    def __init__(self, status: int, ok: bool, payload: bytes) -> None:
        self.status = status
        self.ok = ok
        self._payload = payload

    def body(self) -> bytes:
        return self._payload


class _FakeContext:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append((url, headers))
        return self.response


def test_download_media_via_context(tmp_path: Path) -> None:
    ctx_ok = _FakeContext(_FakeResponse(200, True, b"image-bytes"))
    dest = tmp_path / "dl" / "pic.jpg"
    out = download_media_via_context(
        ctx_ok,
        "https://cdn.example.com/pic.jpg",
        dest,
        headers={"Referer": "https://www.instagram.com/"},
    )
    assert out == dest
    assert dest.read_bytes() == b"image-bytes"
    assert ctx_ok.calls[0][1] == {"Referer": "https://www.instagram.com/"}

    ctx_err = _FakeContext(_FakeResponse(403, False, b"forbidden"))
    with pytest.raises(RuntimeError):
        download_media_via_context(ctx_err, "https://cdn.example.com/403.jpg", tmp_path / "fail.jpg")

    ctx_empty = _FakeContext(_FakeResponse(200, True, b""))
    with pytest.raises(RuntimeError):
        download_media_via_context(ctx_empty, "https://cdn.example.com/empty.jpg", tmp_path / "empty.jpg")
