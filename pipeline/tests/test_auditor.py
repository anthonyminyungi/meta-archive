"""Tests for severity-based integrity auditor (ERROR/WARN gate, histograms, cross-checks)."""

from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.schema.models import (
    MediaItem,
    Post,
    Profile,
    ProfileCounts,
    Story,
)
from meta_archive_pipeline.verify.auditor import audit_directories, audit_sources


def _make_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color=(20, 120, 210)).save(path, format="JPEG")


def _synthetic_mp4(*, include_audio: bool) -> bytes:
    def box(t: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), t) + payload

    def hdlr(h: bytes) -> bytes:
        return box(b"hdlr", b"\x00\x00\x00\x00\x00\x00\x00\x00" + h)

    traks = [box(b"trak", box(b"mdia", hdlr(b"vide")))]
    if include_audio:
        traks.append(box(b"trak", box(b"mdia", hdlr(b"soun"))))
    return (
        box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + box(b"moov", b"".join(traks))
        + box(b"mdat", b"\x00" * 16)
    )


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_auditor_clean_export_passes_gate(tmp_path: Path) -> None:
    root = tmp_path / "ig_clean"
    _make_jpeg(root / "media" / "posts" / "101.jpg")
    mp4_path = root / "media" / "posts" / "102.mp4"
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    mp4_path.write_bytes(_synthetic_mp4(include_audio=True))

    _write_json(
        root / "your_instagram_activity" / "media" / "posts_1.json",
        [
            {"creation_timestamp": 1700000000, "media": [{"uri": "media/posts/101.jpg"}]},
            {"creation_timestamp": 1702500000, "media": [{"uri": "media/posts/102.mp4"}]},
        ],
    )

    report = audit_directories(ig_official=root)
    assert report.passed_gate is True
    assert report.errors == []
    assert report.warnings == []
    assert report.stats["official_posts"] == 2


def test_auditor_errors_fail_gate(tmp_path: Path) -> None:
    root = tmp_path / "ig_broken"

    # 1. Zero-byte image
    zero_file = root / "media" / "posts" / "zero.jpg"
    zero_file.parent.mkdir(parents=True, exist_ok=True)
    zero_file.write_bytes(b"")

    # 2. Corrupt image
    bad_img = root / "media" / "posts" / "corrupt.jpg"
    bad_img.write_bytes(b"not a jpeg")

    # 3. Corrupt mp4
    bad_mp4 = root / "media" / "posts" / "corrupt.mp4"
    bad_mp4.write_bytes(b"not an mp4 container")

    # 4. Broken JSON file on disk
    broken_json = root / "ads_information" / "broken.json"
    broken_json.parent.mkdir(parents=True, exist_ok=True)
    broken_json.write_text("{broken json", encoding="utf-8")

    # 5. Missing media referenced by bundle
    bundle = OfficialExportBundle(
        platform="instagram",
        root_dir=root,
        posts=[
            Post(
                platform="instagram",
                post_id="p1",
                timestamp_unix=1700000000,
                media=[
                    MediaItem(media_type="image", local_path="media/posts/missing.jpg"),
                    MediaItem(media_type="image", local_path="media/posts/zero.jpg"),
                    MediaItem(media_type="image", local_path="media/posts/corrupt.jpg"),
                    MediaItem(media_type="video", local_path="media/posts/corrupt.mp4"),
                ],
            )
        ],
    )

    report = audit_sources(official_bundles=[bundle])
    assert report.passed_gate is False
    codes = {e.code for e in report.errors}
    assert codes == {
        "JSON_PARSE_ERROR",
        "MISSING_MEDIA",
        "ZERO_BYTE_FILE",
        "CORRUPT_IMAGE",
        "CORRUPT_VIDEO",
    }


def test_auditor_warnings_timeline_gaps_and_cross_check(tmp_path: Path) -> None:
    root = tmp_path / "ig_warn"

    silent_unmuted = root / "media" / "posts" / "silent1.mp4"
    silent_muted = root / "media" / "posts" / "silent2.mp4"
    orphan = root / "media" / "posts" / "orphan.jpg"
    silent_unmuted.parent.mkdir(parents=True, exist_ok=True)
    silent_unmuted.write_bytes(_synthetic_mp4(include_audio=False))
    silent_muted.write_bytes(_synthetic_mp4(include_audio=False))
    _make_jpeg(orphan)

    ts_jan = int(datetime(2024, 1, 10, tzinfo=timezone.utc).timestamp())
    ts_jun = int(datetime(2024, 6, 10, tzinfo=timezone.utc).timestamp())  # Feb..May = 4 month gap

    bundle = OfficialExportBundle(
        platform="instagram",
        root_dir=root,
        posts=[
            Post(
                platform="instagram",
                post_id="p_jan",
                timestamp_unix=ts_jan,
                media=[
                    MediaItem(media_type="video", local_path="media/posts/silent1.mp4", is_audio_muted=None),
                    MediaItem(media_type="video", local_path="media/posts/silent2.mp4", is_audio_muted=True),
                ],
            ),
            Post(
                platform="instagram",
                post_id="p_jun",
                timestamp_unix=ts_jun,
                media=[],
            ),
        ],
        stories=[
            Story(
                platform="instagram",
                story_id="st_empty",
                timestamp_unix=ts_jan,
                media=None,
            )
        ],
    )

    collected_profile = Profile(
        platform="instagram",
        username="yungis_",
        counts=ProfileCounts(posts=5),
    )
    collected_posts = [
        Post(platform="instagram", post_id="pw_1", timestamp_unix=ts_jan, source="playwright")
    ]

    report = audit_sources(
        official_bundles=[bundle],
        collected_profiles=[collected_profile],
        collected_posts=collected_posts,
    )

    assert report.passed_gate is True
    warn_codes = [w.code for w in report.warnings]
    assert "VIDEO_NO_AUDIO" in warn_codes
    # Only silent1 (is_audio_muted=None) triggers VIDEO_NO_AUDIO; silent2 (is_audio_muted=True) does not
    no_audio_paths = [w.path for w in report.warnings if w.code == "VIDEO_NO_AUDIO"]
    assert no_audio_paths == ["media/posts/silent1.mp4"]

    assert "EMPTY_MEDIA_URI" in warn_codes
    assert "ORPHAN_MEDIA" in warn_codes
    assert "TIMELINE_GAP" in warn_codes
    assert "COUNT_MISMATCH" in warn_codes

    hist = report.year_month_histogram["instagram:posts"]
    assert hist == {
        "2024-01": 1,
        "2024-02": 0,
        "2024-03": 0,
        "2024-04": 0,
        "2024-05": 0,
        "2024-06": 1,
    }
    assert report.zero_month_gaps["instagram:posts"] == ["2024-02..2024-05 (4m)"]

    assert len(report.cross_checks) == 1
    cc = report.cross_checks[0]
    assert cc.live_profile_posts == 5
    assert cc.playwright_posts == 1
    assert cc.official_active_posts == 2
    assert cc.delta_live_vs_playwright == 4
    assert cc.delta_official_vs_playwright == 1

    as_dict = report.to_dict()
    assert as_dict["passed_gate"] is True
    md = report.format_markdown()
    assert "PASS" in md
    assert "3중 교차 대조" in md
