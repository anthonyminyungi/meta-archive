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


def _audio_only_mp4() -> bytes:
    def box(t: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), t) + payload

    def hdlr(h: bytes) -> bytes:
        return box(b"hdlr", b"\x00\x00\x00\x00\x00\x00\x00\x00" + h)

    return (
        box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + box(b"moov", box(b"trak", box(b"mdia", hdlr(b"soun"))))
        + box(b"mdat", b"\x00" * 16)
    )


def test_auditor_full_root_orphan_mp4_voice_audio_fb_e2ee_warn_and_tagged_posts(tmp_path: Path) -> None:
    fb_root = tmp_path / "fb_official"

    # Audio-only .mp4 voice clip in Messenger
    voice_mp4 = fb_root / "your_facebook_activity" / "messages" / "inbox" / "r1" / "audio" / "voice.mp4"
    voice_mp4.parent.mkdir(parents=True, exist_ok=True)
    voice_mp4.write_bytes(_audio_only_mp4())

    # Unreferenced orphan file outside `<root>/media/` (e.g. `files/orphan_doc.pdf`)
    orphan_pdf = fb_root / "files" / "orphan_doc.pdf"
    orphan_pdf.parent.mkdir(parents=True, exist_ok=True)
    orphan_pdf.write_bytes(b"%PDF-1.4\norphan")

    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "inbox" / "r1" / "message_1.json",
        {
            "title": "친구",
            "participants": [{"name": "친구"}],
            "messages": [
                {
                    "sender_name": "친구",
                    "timestamp_ms": 1700000000000,
                    "audio_files": [
                        {"uri": "your_facebook_activity/messages/inbox/r1/audio/voice.mp4"}
                    ],
                }
            ],
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "secret_conversations.json",
        {
            "secret_conversations_v2": {
                "armadillo_devices": [{"device_model": "SM-S928N", "last_active_time": 1750000000}]
            }
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_posts_1.json",
        [{"timestamp": 1750000000, "data": [{"post": "최근 글"}]}],
    )
    _make_jpeg(fb_root / "your_facebook_activity" / "posts" / "media" / "uncat.jpg")
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_uncategorized_photos.json",
        {"other_photos_v2": [{"uri": "your_facebook_activity/posts/media/uncat.jpg", "creation_timestamp": 1740000000}]},
    )

    # IG collected directory with 1 own post + 1 tagged post -> live profile count=1 must NOT mismatch!
    ig_col = tmp_path / "ig_collected"
    _make_jpeg(ig_col / "media" / "posts" / "SC1_01.jpg")
    _make_jpeg(ig_col / "media" / "tagged" / "SCTAG_01.jpg")
    _write_json(
        ig_col / "profile.json",
        {"platform": "instagram", "username": "yungis_", "counts": {"posts": 1}},
    )
    _write_json(
        ig_col / "posts" / "SC1.json",
        {
            "platform": "instagram",
            "post_id": "1",
            "shortcode": "SC1",
            "post_type": "post",
            "timestamp_unix": 1700000000,
            "media": [{"media_type": "image", "local_path": "media/instagram/posts/SC1_01.jpg"}],
            "source": "playwright",
        },
    )
    _write_json(
        ig_col / "tagged_posts" / "SCTAG.json",
        {
            "platform": "instagram",
            "post_id": "2",
            "shortcode": "SCTAG",
            "post_type": "tagged",
            "timestamp_unix": 1700000500,
            "media": [{"media_type": "image", "local_path": "media/instagram/tagged/SCTAG_01.jpg"}],
            "source": "playwright",
        },
    )

    report = audit_directories(fb_official=fb_root, ig_collected=ig_col)
    assert report.passed_gate is True
    warn_codes = {w.code for w in report.warnings}
    assert "VIDEO_NO_AUDIO" not in warn_codes
    assert "FB_E2EE_CUTOFF_SUSPECTED" in warn_codes
    assert "ORPHAN_MEDIA" in warn_codes
    orphan_paths = [w.path for w in report.warnings if w.code == "ORPHAN_MEDIA"]
    assert "files/orphan_doc.pdf" in orphan_paths

    # IG live_profile_posts=1 vs own playwright_posts=1 -> no COUNT_MISMATCH for instagram!
    mismatch_platforms = {w.details.get("platform") for w in report.warnings if w.code == "COUNT_MISMATCH"}
    assert "instagram" not in mismatch_platforms

    fb_cc = next(c for c in report.cross_checks if c.platform == "facebook")
    assert fb_cc.official_timeline_posts == 1
    assert fb_cc.official_nontimeline_posts == 1

    # Explicitly passed non-existent directory must fail gate with MISSING_SOURCE_DIR!
    missing_rep = audit_directories(ig_official=tmp_path / "does_not_exist")
    assert missing_rep.passed_gate is False
    assert any(e.code == "MISSING_SOURCE_DIR" for e in missing_rep.errors)
