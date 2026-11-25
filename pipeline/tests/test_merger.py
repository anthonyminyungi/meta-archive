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
    SharedPostRef,
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


def test_merge_shared_link_posts_by_url_and_materialize_thumbnail(tmp_path: Path) -> None:
    pw_root = tmp_path / "collected"
    thumb_src = pw_root / "media" / "links" / "fb1_thumb.jpg"
    _pattern_image(thumb_src, variant="mountain")
    orig_sha = sha256_file(thumb_src)

    ts = 1712000000
    off_post = Post(
        platform="facebook",
        post_id="fb_off_shared",
        post_type="shared",
        timestamp_unix=ts,
        caption="",
        shared_post=SharedPostRef(
            url="https://news.example.com/article/42/",
            title="공유 기사 제목",
            domain="news.example.com",
        ),
        source="official_export",
    )
    pw_post = Post(
        platform="facebook",
        post_id="fb_pw_shared",
        post_type="shared",
        timestamp_unix=ts + 15,
        caption="",
        comments=[Comment(comment_id="fc1", author=UserRef(full_name="친구"), text="좋은 기사네")],
        shared_post=SharedPostRef(
            url="https://l.facebook.com/l.php?u=https%3A%2F%2Fnews.example.com%2Farticle%2F42%3Ffbclid%3Dxyz",
            title="공유 기사 제목",
            text="요약 설명",
            domain="news.example.com",
            thumbnail_local_path="media/facebook/links/fb1_thumb.jpg",
        ),
        source="playwright",
    )

    merged, amb = merge_posts([off_post], [pw_post], playwright_roots=[pw_root])
    assert amb == []
    assert len(merged) == 1
    p = merged[0]
    assert p.source == "merged"
    assert p.post_type == "shared"
    assert p.shared_post is not None
    assert p.shared_post.text == "요약 설명"
    assert p.shared_post.thumbnail_local_path == "media/facebook/links/fb1_thumb.jpg"
    assert p.comments[0].text == "좋은 기사네"

    from meta_archive_pipeline.build.merger import MergeResult

    out_dir = tmp_path / "archive"
    summary = materialize_archive(MergeResult(posts=merged), out_dir, source_roots=[pw_root])
    assert summary.media_copied == 1
    assert summary.xmp_written == 0
    dest_thumb = out_dir / "media" / "facebook" / "links" / "fb1_thumb.jpg"
    assert dest_thumb.exists()
    assert sha256_file(dest_thumb) == orig_sha
    assert not (out_dir / "media" / "facebook" / "links" / "fb1_thumb.jpg.xmp").exists()


def test_materialize_avif_jpeg_noext_collision_hash_stickers_taken_unix_and_video_skew(
    tmp_path: Path,
) -> None:
    from meta_archive_pipeline.build.merger import merge_all
    from meta_archive_pipeline.schema.models import ActivityItem, Message, MessageThread

    off_root = tmp_path / "fb_off"

    # 1. Disguised .avif with JPEG magic bytes + integer taken_at_unix
    avif_jpeg = off_root / "your_facebook_activity" / "posts" / "media" / "cam1.avif"
    _pattern_image(avif_jpeg, variant="mountain")
    avif_sha = sha256_file(avif_jpeg)

    # 2. Extensionless JPEG in Messenger & extensionless PDF in group post
    noext_jpg = off_root / "your_facebook_activity" / "messages" / "inbox" / "r1" / "photos" / "noext_photo"
    _pattern_image(noext_jpg, variant="checker")
    noext_jpg_sha = sha256_file(noext_jpg)

    noext_pdf = off_root / "files" / "204712957226567"
    noext_pdf.parent.mkdir(parents=True, exist_ok=True)
    noext_pdf.write_bytes(b"%PDF-1.3\n1 0 obj\n<<>>\nendobj\n")
    noext_pdf_sha = sha256_file(noext_pdf)

    # 3. Same filename `shared_name.jpg` in two different Messenger folders with DIFFERENT contents
    dup_a = off_root / "your_facebook_activity" / "messages" / "inbox" / "r1" / "photos" / "shared_name.jpg"
    dup_b = off_root / "your_facebook_activity" / "messages" / "inbox" / "r2" / "photos" / "shared_name.jpg"
    _pattern_image(dup_a, variant="mountain", size=(64, 64))
    _pattern_image(dup_b, variant="checker", size=(64, 64))
    sha_a = sha256_file(dup_a)
    sha_b = sha256_file(dup_b)
    assert sha_a != sha_b

    # 4. Shared sticker PNG in `stickers_used/` -> must land in `stickers/` without `.xmp`
    sticker_png = off_root / "your_facebook_activity" / "messages" / "stickers_used" / "st1.png"
    sticker_png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (32, 32), color=(255, 0, 0, 128)).save(sticker_png, format="PNG")

    taken_ts = 1599990000
    upload_ts = 1600000500

    bundle = OfficialExportBundle(
        platform="facebook",
        root_dir=off_root,
        posts=[
            Post(
                platform="facebook",
                post_id="fb_p1",
                timestamp_unix=upload_ts,
                media=[
                    MediaItem(
                        media_type="image",
                        local_path="your_facebook_activity/posts/media/cam1.avif",
                        taken_at_unix=taken_ts,
                    )
                ],
            ),
            Post(
                platform="facebook",
                post_id="fb_grp1",
                post_type="group_post",
                group_name="컴공 학회",
                timestamp_unix=1580000000,
                media=[MediaItem(media_type="unknown", local_path="files/204712957226567")],
            ),
        ],
        messages=[
            MessageThread(
                platform="facebook",
                thread_id="inbox/r1",
                messages=[
                    Message(
                        sender="A",
                        timestamp_unix=1700000000,
                        media=[
                            MediaItem(
                                media_type="image",
                                local_path="your_facebook_activity/messages/inbox/r1/photos/noext_photo",
                            ),
                            MediaItem(
                                media_type="image",
                                local_path="your_facebook_activity/messages/inbox/r1/photos/shared_name.jpg",
                            ),
                            MediaItem(
                                media_type="image",
                                local_path="your_facebook_activity/messages/stickers_used/st1.png",
                            ),
                        ],
                    )
                ],
            ),
            MessageThread(
                platform="facebook",
                thread_id="inbox/r2",
                messages=[
                    Message(
                        sender="B",
                        timestamp_unix=1700000100,
                        media=[
                            MediaItem(
                                media_type="image",
                                local_path="your_facebook_activity/messages/inbox/r2/photos/shared_name.jpg",
                            ),
                        ],
                    )
                ],
            ),
        ],
        activities=[
            ActivityItem(
                activity_id="fb_act_1",
                platform="facebook",
                kind="own_comment",
                timestamp_unix=1650000000,
                text="스티커 댓글",
                media=[
                    MediaItem(
                        media_type="image",
                        local_path="your_facebook_activity/messages/stickers_used/st1.png",
                    )
                ],
            )
        ],
    )

    merged = merge_all(official_bundles=[bundle])
    out_dir = tmp_path / "archive_norm"
    summary = materialize_archive(merged, out_dir, source_roots=[off_root], tz_offset="+09:00")
    paths = ArchivePaths(out_dir)

    assert paths.activities_json.exists()
    assert summary.activities_count == 1

    # 1. Disguised .avif -> .jpg losslessly, mtime & XMP use `taken_ts` (not `upload_ts`)
    cam_post = next(p for p in merged.posts if p.post_id == "fb_p1")
    assert cam_post.media[0].local_path == "media/facebook/posts/cam1.jpg"
    cam_dest = out_dir / "media" / "facebook" / "posts" / "cam1.jpg"
    assert cam_dest.exists()
    assert sha256_file(cam_dest) == avif_sha
    assert int(os.stat(cam_dest).st_mtime) == taken_ts
    cam_xmp = out_dir / "media" / "facebook" / "posts" / "cam1.jpg.xmp"
    assert cam_xmp.exists()

    # 2. Extensionless JPEG -> .jpg & extensionless PDF -> .pdf
    grp_post = next(p for p in merged.posts if p.post_id == "fb_grp1")
    assert grp_post.media[0].local_path == "media/facebook/posts/204712957226567.pdf"
    assert sha256_file(out_dir / "media" / "facebook" / "posts" / "204712957226567.pdf") == noext_pdf_sha

    r1_media = merged.messages[0].messages[0].media
    assert r1_media[0].local_path == "media/facebook/messages/noext_photo.jpg"
    assert sha256_file(out_dir / "media" / "facebook" / "messages" / "noext_photo.jpg") == noext_jpg_sha

    # 3. Collision disambiguation preserves BOTH distinct files!
    r2_media = merged.messages[1].messages[0].media
    assert r1_media[1].local_path != r2_media[0].local_path
    assert sha256_file(out_dir.joinpath(*r1_media[1].local_path.split("/"))) == sha_a
    assert sha256_file(out_dir.joinpath(*r2_media[0].local_path.split("/"))) == sha_b

    # 4. Sticker lands in `stickers/` with zero XMP sidecar
    assert r1_media[2].local_path == "media/facebook/stickers/st1.png"
    assert not (out_dir / "media" / "facebook" / "stickers" / "st1.png.xmp").exists()

    # 5. Captionless single-video post with 120s encoding skew merges cleanly
    off_vid_post = Post(
        platform="instagram",
        post_id="ig_off_v",
        timestamp_unix=1700000000,
        caption="",
        media=[MediaItem(media_type="video", local_path="media/posts/v1.mp4")],
        source="official_export",
    )
    pw_vid_post = Post(
        platform="instagram",
        post_id="ig_pw_v",
        shortcode="SC_VID_SKEW",
        post_type="reel",
        timestamp_unix=1700000120,  # 120s > 60s skew, empty caption
        caption="",
        media=[MediaItem(media_type="video", local_path="media/instagram/posts/SC_VID_SKEW_01.mp4")],
        source="playwright",
    )
    v_merged, v_amb = merge_posts([off_vid_post], [pw_vid_post])
    assert v_amb == []
    assert len(v_merged) == 1
    assert v_merged[0].source == "merged"


def test_fb_official_duplicate_empty_share_stubs_dedup_and_merge_with_playwright(
    tmp_path: Path,
) -> None:
    from meta_archive_pipeline.official.fb_parser import parse_fb_official_export

    fb_dir = tmp_path / "fb_export"
    posts_dir = fb_dir / "your_facebook_activity" / "posts"
    posts_dir.mkdir(parents=True)
    # Two identical empty share stubs at the exact same timestamp (common in FB official exports)
    raw_entries = [
        {
            "timestamp": 1738604126,
            "title": "민윤기님이 게시물을 공유했습니다.",
            "attachments": [{"data": [{"external_context": {}}]}],
        },
        {
            "timestamp": 1738604126,
            "title": "민윤기님이 게시물을 공유했습니다.",
            "attachments": [{"data": [{"external_context": {}}]}],
        },
    ]
    (posts_dir / "your_posts_1.json").write_text(
        json.dumps(raw_entries, ensure_ascii=False), encoding="utf-8"
    )

    bundle = parse_fb_official_export(fb_dir)
    assert len(bundle.posts) == 1
    off_stub = bundle.posts[0]
    assert off_stub.post_type == "shared"
    assert off_stub.shared_post is None

    pw_post = Post(
        platform="facebook",
        post_id="3361571257310715",
        post_type="shared",
        timestamp_unix=1738604127,  # ±1s skew
        caption="",
        shared_post=SharedPostRef(
            url="https://www.facebook.com/groups/550/posts/1792/",
            author_name="임진호",
            text="원글 본문",
            domain="facebook.com",
            thumbnail_local_path="media/facebook/links/3361571257310715_thumb.jpg",
        ),
        source="playwright",
    )
    merged_posts, amb = merge_posts([off_stub], [pw_post])
    assert amb == []
    assert len(merged_posts) == 1
    assert merged_posts[0].source == "merged"
    assert merged_posts[0].post_id == "3361571257310715"
    assert merged_posts[0].activity_title == "민윤기님이 게시물을 공유했습니다."
    assert merged_posts[0].shared_post is not None
    assert merged_posts[0].shared_post.author_name == "임진호"
    assert merged_posts[0].shared_post.thumbnail_local_path == "media/facebook/links/3361571257310715_thumb.jpg"


def test_fb_exact_empty_status_stubs_mention_markup_and_group_isolation() -> None:
    ts = 1722346904
    # 1. Empty status update stub with ts_delta == 0 merges cleanly
    off_status = Post(
        platform="facebook",
        post_id="fb_official_1",
        post_type="post",
        timestamp_unix=ts,
        caption="",
        activity_title="민윤기님이 상태를 업데이트했습니다.",
        source="official_export",
    )
    pw_status = Post(
        platform="facebook",
        post_id="3162337327234110",
        post_type="post",
        timestamp_unix=ts,
        caption="",
        source="playwright",
    )
    # 2. Facebook @[id:type:Name] mention markup normalizes to Name
    off_mention = Post(
        platform="facebook",
        post_id="fb_official_2",
        post_type="shared",
        timestamp_unix=ts - 1000,
        caption="@[100007046917539:2048:김재원]",
        source="official_export",
    )
    pw_mention = Post(
        platform="facebook",
        post_id="674555589345642",
        post_type="shared",
        timestamp_unix=ts - 1000,
        caption="김재원",
        source="playwright",
    )
    # 3. Official group_post does not steal a non-group Playwright timeline post
    off_group = Post(
        platform="facebook",
        post_id="fb_group_1",
        post_type="group_post",
        group_name="테스트그룹",
        timestamp_unix=ts - 2000 + 53,
        caption="http://soundshub.cf 공유 테스트",
        source="official_export",
    )
    off_timeline = Post(
        platform="facebook",
        post_id="fb_official_3",
        post_type="shared",
        timestamp_unix=ts - 2000,
        caption="http://soundshub.cf 공유 테스트",
        source="official_export",
    )
    pw_timeline = Post(
        platform="facebook",
        post_id="1594486164019242",
        post_type="shared",
        timestamp_unix=ts - 2000,
        caption="http://soundshub.cf 공유 테스트",
        source="playwright",
    )

    merged_posts, amb = merge_posts(
        [off_status, off_mention, off_group, off_timeline],
        [pw_status, pw_mention, pw_timeline],
    )
    assert amb == []
    assert len(merged_posts) == 4
    by_id = {p.post_id: p for p in merged_posts}
    assert by_id["3162337327234110"].source == "merged"
    assert by_id["3162337327234110"].activity_title == "민윤기님이 상태를 업데이트했습니다."
    assert by_id["674555589345642"].source == "merged"
    assert by_id["1594486164019242"].source == "merged"
    assert by_id["fb_group_1"].source == "official_export"
