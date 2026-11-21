"""Tests for perceptual dHash matching, deterministic merging, and archive materialization."""

from __future__ import annotations

import json
import os
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

from meta_archive_pipeline.build.merger import (
    compute_dhash,
    hamming_distance,
    materialize_archive,
    merge_connections,
    merge_posts,
    merge_stories,
)
from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    Location,
    MediaItem,
    Post,
    Story,
    UserRef,
)
from meta_archive_pipeline.schema.paths import ArchivePaths
from meta_archive_pipeline.utils.fs import sha256_file


def _pattern_image(path: Path, *, variant: str, size: tuple[int, int] = (128, 128), quality: int = 95) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    if variant == "mountain":
        draw.rectangle([0, 0, size[0], size[1] // 2], fill=(40, 110, 220))
        draw.polygon([(0, size[1]), (size[0] // 2, size[1] // 4), (size[0], size[1])], fill=(20, 140, 60))
    else:
        for i in range(0, size[0], 16):
            for j in range(0, size[1], 16):
                if ((i + j) // 16) % 2 == 0:
                    draw.rectangle([i, j, i + 15, j + 15], fill=(10, 10, 10))
    img.save(path, format="JPEG", quality=quality)


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


def test_compute_dhash_and_hamming_distance(tmp_path: Path) -> None:
    p1 = tmp_path / "m_orig.jpg"
    p2 = tmp_path / "m_resized.jpg"
    p3 = tmp_path / "checker.jpg"

    _pattern_image(p1, variant="mountain", size=(200, 200), quality=95)
    _pattern_image(p2, variant="mountain", size=(80, 80), quality=55)
    _pattern_image(p3, variant="checker", size=(200, 200), quality=95)

    h1 = compute_dhash(p1)
    h2 = compute_dhash(p2)
    h3 = compute_dhash(p3)
    assert h1 is not None and h2 is not None and h3 is not None
    assert hamming_distance(h1, h2) <= 10
    assert hamming_distance(h1, h3) > 10


def test_merge_posts_matches_by_dhash_and_prefers_audio_mp4(tmp_path: Path) -> None:
    off_root = tmp_path / "official"
    pw_root = tmp_path / "collected"

    _pattern_image(off_root / "media" / "posts" / "off_1.jpg", variant="mountain", size=(160, 160))
    _pattern_image(pw_root / "media" / "posts" / "SC1_01.jpg", variant="mountain", size=(96, 96), quality=70)

    silent_mp4 = off_root / "media" / "posts" / "off_vid.mp4"
    audio_mp4 = pw_root / "media" / "posts" / "SC2_01.mp4"
    silent_mp4.parent.mkdir(parents=True, exist_ok=True)
    audio_mp4.parent.mkdir(parents=True, exist_ok=True)
    silent_mp4.write_bytes(_synthetic_mp4(include_audio=False))
    audio_mp4.write_bytes(_synthetic_mp4(include_audio=True))

    off_posts = [
        Post(
            platform="instagram",
            post_id="ig_off_1",
            timestamp_unix=1700000000,
            caption="설악산 등반 기록",
            location=Location(name="설악산", lat=38.119, lng=128.465),
            media=[
                MediaItem(
                    media_type="image",
                    local_path="media/posts/off_1.jpg",
                    taken_at_exif="2023:11:14 09:15:00",
                    gps_lat=38.119,
                    gps_lng=128.465,
                )
            ],
            source="official_export",
        ),
        Post(
            platform="instagram",
            post_id="ig_off_2",
            timestamp_unix=1700050000,
            caption="음악 들어간 릴스",
            media=[MediaItem(media_type="video", local_path="media/posts/off_vid.mp4")],
            source="official_export",
        ),
    ]

    pw_posts = [
        Post(
            platform="instagram",
            post_id="pw_1",
            shortcode="SC1",
            url="https://www.instagram.com/p/SC1/",
            timestamp_unix=1700000018,  # 18s clock skew
            caption="설악산 등반 기록!",
            likes=LikesInfo(count=25),
            comments=[Comment(comment_id="c1", author=UserRef(username="alice"), text="멋지다!")],
            media=[MediaItem(media_type="image", local_path="media/instagram/posts/SC1_01.jpg")],
            source="playwright",
        ),
        Post(
            platform="instagram",
            post_id="pw_2",
            shortcode="SC2",
            url="https://www.instagram.com/reel/SC2/",
            post_type="reel",
            timestamp_unix=1700050005,
            caption="음악 들어간 릴스",
            media=[MediaItem(media_type="video", local_path="media/instagram/posts/SC2_01.mp4", has_audio=True)],
            source="playwright",
        ),
    ]

    merged, ambiguous = merge_posts(
        off_posts,
        pw_posts,
        official_roots=[off_root],
        playwright_roots=[pw_root],
    )
    assert ambiguous == []
    assert len(merged) == 2

    by_sc = {p.shortcode: p for p in merged}
    p1 = by_sc["SC1"]
    assert p1.source == "merged"
    assert p1.likes.count == 25
    assert p1.comments[0].text == "멋지다!"
    assert p1.location is not None and p1.location.name == "설악산"
    assert p1.media[0].local_path == "media/instagram/posts/SC1_01.jpg"
    assert p1.media[0].taken_at_exif == "2023:11:14 09:15:00"
    assert p1.media[0].gps_lat == 38.119

    p2 = by_sc["SC2"]
    assert p2.source == "merged"
    assert p2.media[0].local_path == "media/instagram/posts/SC2_01.mp4"
    assert p2.media[0].has_audio is True


def test_ambiguous_post_ties_and_duplicate_fb_friend_names(tmp_path: Path) -> None:
    # Identical timestamp & caption & identical image across 2 Playwright candidates -> tie!
    off_root = tmp_path / "off"
    pw_root = tmp_path / "pw"
    _pattern_image(off_root / "media/posts/o.jpg", variant="mountain")
    _pattern_image(pw_root / "media/posts/a.jpg", variant="mountain")
    _pattern_image(pw_root / "media/posts/b.jpg", variant="mountain")

    off_posts = [
        Post(
            platform="instagram",
            post_id="off_dup",
            timestamp_unix=1700000000,
            caption="같은 글",
            media=[MediaItem(media_type="image", local_path="media/posts/o.jpg")],
        )
    ]
    pw_posts = [
        Post(
            platform="instagram",
            post_id="pw_a",
            shortcode="SCA",
            timestamp_unix=1700000000,
            caption="같은 글",
            media=[MediaItem(media_type="image", local_path="media/posts/a.jpg")],
            source="playwright",
        ),
        Post(
            platform="instagram",
            post_id="pw_b",
            shortcode="SCB",
            timestamp_unix=1700000000,
            caption="같은 글",
            media=[MediaItem(media_type="image", local_path="media/posts/b.jpg")],
            source="playwright",
        ),
    ]
    merged, post_amb = merge_posts(
        off_posts, pw_posts, official_roots=[off_root], playwright_roots=[pw_root]
    )
    assert len(post_amb) == 1
    assert post_amb[0].reason == "tied_or_contested_post_match"
    assert len(merged) == 3  # None silently merged

    # Duplicate Facebook friend names ("홍길동" x2) must not be force-merged
    conns, conn_amb = merge_connections(
        [
            Connection(platform="facebook", relation="friend", full_name="홍길동", timestamp_unix=1600000000),
            Connection(platform="facebook", relation="friend", full_name="유일한친구", timestamp_unix=1600000001),
        ],
        [
            Connection(platform="facebook", relation="friend", user_id="fb1", full_name="홍길동"),
            Connection(platform="facebook", relation="friend", user_id="fb2", full_name="홍길동"),
            Connection(platform="facebook", relation="friend", user_id="fb3", full_name="유일한친구"),
        ],
    )
    assert len(conn_amb) == 1
    assert conn_amb[0].reason == "duplicate_facebook_friend_name"
    unique_friend = next(c for c in conns if c.full_name == "유일한친구")
    assert unique_friend.match_confidence == "heuristic"
    assert unique_friend.user_id == "fb3"
    hong_entries = [c for c in conns if c.full_name == "홍길동"]
    assert len(hong_entries) == 3
    assert all(c.match_confidence == "unmatched" for c in hong_entries)


def test_materialize_archive_lossless_xmp_and_mtime(tmp_path: Path) -> None:
    off_root = tmp_path / "official"
    src_img = off_root / "media" / "posts" / "202311" / "pic.jpg"
    _pattern_image(src_img, variant="mountain")
    orig_sha = sha256_file(src_img)

    ts_post = 1700000000
    bundle = OfficialExportBundle(
        platform="instagram",
        root_dir=off_root,
        posts=[
            Post(
                platform="instagram",
                post_id="p1",
                timestamp_unix=ts_post,
                caption="제주 여행",
                location=Location(name="제주", lat=33.4996, lng=126.5312),
                media=[
                    MediaItem(
                        media_type="image",
                        local_path="media/posts/202311/pic.jpg",
                        taken_at_exif="2023:11:14 15:20:00",
                    )
                ],
            )
        ],
        stories=merge_stories(
            [
                Story(
                    platform="instagram",
                    story_id="s1",
                    timestamp_unix=ts_post,
                    media=None,
                )
            ],
            [],
        ),
    )

    from meta_archive_pipeline.build.merger import merge_all

    merged = merge_all(official_bundles=[bundle])
    out_dir = tmp_path / "archive"
    summary = materialize_archive(merged, out_dir, source_roots=[off_root], tz_offset="+09:00")

    assert summary.posts_count == 1
    assert summary.media_copied == 1
    assert summary.xmp_written == 1

    paths = ArchivePaths(out_dir)
    copied_img = paths.root / "media" / "instagram" / "posts" / "202311" / "pic.jpg"
    sidecar = paths.root / "media" / "instagram" / "posts" / "202311" / "pic.jpg.xmp"
    assert copied_img.exists()
    assert sha256_file(copied_img) == orig_sha
    assert int(os.stat(copied_img).st_mtime) == ts_post
    assert sidecar.exists()

    root_xml = ET.fromstring(sidecar.read_text(encoding="utf-8"))
    ns = {"exif": "http://ns.adobe.com/exif/1.0/"}
    assert root_xml.find(".//exif:DateTimeOriginal", ns).text == "2023-11-14T15:20:00+09:00"

    posts_saved = json.loads(paths.posts_json.read_text(encoding="utf-8"))
    assert posts_saved[0]["media"][0]["local_path"] == "media/instagram/posts/202311/pic.jpg"
    assert posts_saved[0]["media"][0]["xmp_path"] == "media/instagram/posts/202311/pic.jpg.xmp"
    assert posts_saved[0]["media"][0]["sha256"] == orig_sha
    assert paths.ambiguous_matches_path.exists()
