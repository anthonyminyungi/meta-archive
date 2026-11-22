"""Tests for canonical archive schema models and path normalization helpers."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meta_archive_pipeline.schema import (
    ARCHIVE_SCHEMA_VERSION,
    ActivityItem,
    ArchivePaths,
    Comment,
    Connection,
    LikesInfo,
    Location,
    MediaItem,
    Message,
    MessageReaction,
    MessageThread,
    Post,
    Profile,
    ProfileCounts,
    ReactionSummary,
    SharedPostRef,
    Story,
    UserRef,
    normalize_rel_path,
)


def test_normalize_rel_path_slashes_leading_and_nfc() -> None:
    nfd_name = unicodedata.normalize("NFD", "제주여행_사진.jpg")
    nfc_name = unicodedata.normalize("NFC", "제주여행_사진.jpg")
    assert nfd_name != nfc_name

    normalized = normalize_rel_path(rf"\media\\posts\./2024\{nfd_name}")
    assert normalized == f"media/posts/2024/{nfc_name}"
    assert unicodedata.is_normalized("NFC", normalized)


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/",
        ".",
        "../secret.txt",
        "media/../../etc/passwd",
        r"C:\Users\dbstn\photo.jpg",
        "d:relative.jpg",
    ],
)
def test_normalize_rel_path_rejects_unsafe_paths(bad_path: str) -> None:
    with pytest.raises(ValueError):
        normalize_rel_path(bad_path)


def test_archive_paths_layout(tmp_path: Path) -> None:
    paths = ArchivePaths(root=tmp_path / "archive")
    assert ARCHIVE_SCHEMA_VERSION == 1
    assert paths.data_dir == paths.root / "data"
    assert paths.media_dir == paths.root / "media"
    assert paths.reports_dir == paths.root / "reports"
    assert paths.db_path == paths.root / "archive.sqlite"
    assert paths.manifest_path == paths.root / "manifest.sha256"
    assert paths.ambiguous_matches_path == paths.root / "reports" / "ambiguous_matches.json"
    assert paths.profiles_json == paths.root / "data" / "profiles.json"
    assert paths.posts_json == paths.root / "data" / "posts.json"
    assert paths.stories_json == paths.root / "data" / "stories.json"
    assert paths.connections_json == paths.root / "data" / "connections.json"
    assert paths.messages_json == paths.root / "data" / "messages.json"
    assert paths.activities_json == paths.root / "data" / "activities.json"
    assert (
        paths.media_rel_path("instagram", "posts", r"2024\photo.jpg")
        == "media/instagram/posts/2024/photo.jpg"
    )
    with pytest.raises(ValueError):
        paths.media_rel_path("instagram/extra", "posts", "a.jpg")


def test_models_json_round_trip() -> None:
    now = datetime(2025, 4, 9, 12, 30, 0, tzinfo=timezone.utc)

    profile = Profile(
        platform="instagram",
        user_id="12345",
        username="yungis_",
        full_name="윤기",
        avatar_local_path=r"\media\instagram\profile\avatar.jpg",
        counts=ProfileCounts(posts=200, followers=107, following=161),
        exported_at=now,
    )
    assert profile.avatar_local_path == "media/instagram/profile/avatar.jpg"
    assert Profile.model_validate_json(profile.model_dump_json()) == profile

    post = Post(
        platform="facebook",
        post_id="fb_999",
        shortcode=None,
        post_type="album_photo",
        author=UserRef(username="yungis_", full_name="윤기"),
        created_at=now,
        timestamp_unix=int(now.timestamp()),
        caption="제주도 기록 #제주",
        hashtags=["제주"],
        location=Location(name="Jeju", lat=33.4996, lng=126.5312),
        media=[
            MediaItem(
                media_id="m1",
                media_type="image",
                local_path=r"media\instagram\posts\cover.jpg",
                xmp_path=r"media\instagram\posts\cover.jpg.xmp",
                taken_at_exif="2025:04:09 21:30:00",
                is_carousel_cover=True,
                has_audio=None,
                is_audio_muted=False,
            )
        ],
        likes=LikesInfo(count=12, is_truncated=False, users=[UserRef(username="friend1")]),
        reactions=ReactionSummary(total=5, by_type={"LIKE": 3, "LOVE": 2}),
        comments=[
            Comment(
                comment_id="c1",
                author=UserRef(username="alice", full_name="앨리스"),
                text="멋지네요!",
                created_at=now,
                like_count=2,
                replies=[
                    Comment(
                        comment_id="c2",
                        author=UserRef(username="yungis_"),
                        text="감사합니다!",
                        created_at=now,
                    )
                ],
            )
        ],
        album_name="여행 앨범",
        shared_post=SharedPostRef(url="https://example.com", title="링크"),
        is_archived=False,
        is_collab=True,
        source="merged",
    )
    assert post.media[0].local_path == "media/instagram/posts/cover.jpg"
    assert post.media[0].xmp_path == "media/instagram/posts/cover.jpg.xmp"
    assert post.comments[0].replies[0].comment_id == "c2"
    assert Post.model_validate_json(post.model_dump_json()) == post

    empty_story = Story(
        platform="instagram",
        story_id="st_empty_uri",
        created_at=now,
        highlight_cover=r"\media\instagram\highlights\cover.jpg",
        media=None,
    )
    assert empty_story.highlight_cover == "media/instagram/highlights/cover.jpg"
    assert empty_story.media is None
    assert Story.model_validate_json(empty_story.model_dump_json()) == empty_story

    conn = Connection(
        platform="facebook",
        relation="friend",
        full_name="홍길동",
        avatar_local_path=r"media\facebook\avatars\1.jpg",
        match_confidence="heuristic",
    )
    assert Connection.model_validate_json(conn.model_dump_json()) == conn

    thread = MessageThread(
        platform="instagram",
        thread_id="inbox_1",
        title="단톡방",
        avatar_local_path=r"\media\instagram\messages\group.jpg",
        participants=[UserRef(username="yungis_"), UserRef(username="bob")],
        is_e2ee=False,
        is_pending=False,
        messages=[
            Message(
                message_id="msg1",
                sender="bob",
                timestamp_ms=int(now.timestamp() * 1000),
                text="안녕!",
                reactions=[MessageReaction(actor="yungis_", reaction="❤️")],
                shared_link="https://www.instagram.com/reel/XYZ/",
                share_text="공유 캡션",
                share_owner="creator_ig",
                is_missed_call=True,
                call_duration=42,
            )
        ],
    )
    assert thread.avatar_local_path == "media/instagram/messages/group.jpg"
    assert MessageThread.model_validate_json(thread.model_dump_json()) == thread

    group_post = Post(
        platform="facebook",
        post_id="fb_grp_1",
        post_type="group_post",
        group_name="알고리즘 스터디",
        activity_title="윤기님이 알고리즘 스터디 그룹에 게시물을 남겼습니다.",
        media=[MediaItem(media_type="unknown", local_path="files/doc.pdf", taken_at_unix=1700000000)],
    )
    assert group_post.media[0].taken_at_unix == 1700000000
    assert Post.model_validate_json(group_post.model_dump_json()) == group_post

    story_with_caption = Story(
        platform="instagram",
        story_id="st_cap",
        caption="오늘의 하늘",
    )
    assert Story.model_validate_json(story_with_caption.model_dump_json()) == story_with_caption

    act = ActivityItem(
        activity_id="fb_cmt_1",
        platform="facebook",
        kind="own_comment",
        timestamp_unix=1700000000,
        text="축하드려요!",
        title="윤기님이 홍길동님의 게시물에 댓글을 남겼습니다.",
        target_author="홍길동",
        media=[MediaItem(media_type="image", local_path=r"media\facebook\stickers\1.png")],
    )
    assert act.media[0].local_path == "media/facebook/stickers/1.png"
    assert ActivityItem.model_validate_json(act.model_dump_json()) == act
