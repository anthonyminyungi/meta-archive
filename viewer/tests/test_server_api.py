"""Comprehensive API, FTS5/Korean search, HTTP Range streaming, and security tests for meta-archive-viewer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_archive_viewer.app import create_app

_SCHEMA_DDL = """
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    user_id TEXT,
    username TEXT,
    full_name TEXT,
    bio TEXT,
    avatar_local_path TEXT,
    counts_json TEXT,
    exported_at TEXT
);
CREATE TABLE posts (
    post_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    shortcode TEXT,
    url TEXT,
    post_type TEXT,
    author_username TEXT,
    author_name TEXT,
    created_at TEXT,
    timestamp_unix INTEGER,
    caption TEXT,
    hashtags_json TEXT,
    location_name TEXT,
    location_lat REAL,
    location_lng REAL,
    tagged_users_json TEXT,
    likes_count INTEGER,
    likes_is_truncated INTEGER,
    reactions_total INTEGER,
    reactions_json TEXT,
    album_name TEXT,
    shared_post_json TEXT,
    audience TEXT,
    is_archived INTEGER,
    is_collab INTEGER,
    source TEXT,
    raw_json TEXT
);
CREATE TABLE media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    seq INTEGER NOT NULL,
    media_id TEXT,
    media_type TEXT,
    local_path TEXT,
    xmp_path TEXT,
    original_uri TEXT,
    width INTEGER,
    height INTEGER,
    duration_sec REAL,
    has_audio INTEGER,
    is_audio_muted INTEGER,
    taken_at_exif TEXT,
    gps_lat REAL,
    gps_lng REAL,
    sha256 TEXT,
    byte_size INTEGER,
    is_carousel_cover INTEGER
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id TEXT,
    post_id TEXT NOT NULL,
    parent_comment_id TEXT,
    platform TEXT NOT NULL,
    depth INTEGER NOT NULL,
    author_username TEXT,
    author_name TEXT,
    text TEXT,
    created_at TEXT,
    timestamp_unix INTEGER,
    like_count INTEGER,
    media_json TEXT,
    stickers_json TEXT,
    reactions_json TEXT
);
CREATE TABLE likes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    user_id TEXT,
    username TEXT,
    full_name TEXT,
    profile_url TEXT
);
CREATE TABLE stories (
    story_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    created_at TEXT,
    timestamp_unix INTEGER,
    source TEXT,
    highlight_id TEXT,
    highlight_title TEXT,
    highlight_cover TEXT,
    location_name TEXT,
    stickers_json TEXT,
    likes_count INTEGER,
    raw_json TEXT
);
CREATE TABLE connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    relation TEXT NOT NULL,
    user_id TEXT,
    username TEXT,
    full_name TEXT,
    profile_url TEXT,
    avatar_local_path TEXT,
    connected_at TEXT,
    timestamp_unix INTEGER,
    match_confidence TEXT
);
CREATE TABLE message_threads (
    thread_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    title TEXT,
    participants_json TEXT,
    is_e2ee INTEGER,
    is_pending INTEGER,
    message_count INTEGER,
    last_timestamp_unix INTEGER
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    message_id TEXT,
    platform TEXT NOT NULL,
    seq INTEGER NOT NULL,
    sender TEXT,
    timestamp_unix INTEGER,
    timestamp_ms INTEGER,
    created_at TEXT,
    text TEXT,
    reactions_json TEXT,
    reply_to_id TEXT,
    shared_link TEXT,
    is_unsent INTEGER,
    call_duration INTEGER
);
CREATE VIRTUAL TABLE search_fts USING fts5(
    entity_type UNINDEXED,
    entity_id UNINDEXED,
    platform UNINDEXED,
    author,
    body,
    timestamp_unix UNINDEXED,
    tokenize='trigram'
);
"""


def _build_fixture_archive(root: Path, schema_version: str = "1") -> Path:
    media_dir = root / "media"
    (media_dir / "instagram" / "posts").mkdir(parents=True, exist_ok=True)
    (media_dir / "instagram" / "stories").mkdir(parents=True, exist_ok=True)
    (media_dir / "instagram" / "avatars").mkdir(parents=True, exist_ok=True)
    (media_dir / "facebook" / "posts").mkdir(parents=True, exist_ok=True)
    (media_dir / "facebook" / "links").mkdir(parents=True, exist_ok=True)
    (media_dir / "facebook" / "messages").mkdir(parents=True, exist_ok=True)

    (media_dir / "instagram" / "posts" / "jeju.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 30)
    video_bytes = bytes(range(100))
    (media_dir / "instagram" / "posts" / "jeju_clip.mp4").write_bytes(video_bytes)
    (media_dir / "instagram" / "stories" / "hl_01.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"S" * 20)
    (media_dir / "instagram" / "avatars" / "me.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"A" * 16)
    (media_dir / "facebook" / "posts" / "album_cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"F" * 24)
    (media_dir / "facebook" / "links" / "shared_thumb.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"L" * 18)
    (media_dir / "facebook" / "messages" / "dm_photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"M" * 20)

    db_path = root / "archive.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_DDL)
    conn.executemany(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        [
            ("schema_version", schema_version),
            ("built_at", "2026-11-21T05:00:00+00:00"),
            ("pipeline_version", "0.1.0"),
        ],
    )

    conn.execute(
        """
        INSERT INTO profiles (platform, user_id, username, full_name, bio, avatar_local_path, counts_json, exported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "instagram",
            "ig_u1",
            "my_ig",
            "홍길동",
            "제주 여행 기록",
            "media/instagram/avatars/me.jpg",
            json.dumps({"posts": 2, "followers": 10, "following": 20}),
            "2026-11-20T10:00:00+09:00",
        ),
    )

    # Posts: IG active, IG archived, FB album post
    conn.execute(
        """
        INSERT INTO posts (
            post_id, platform, shortcode, url, post_type, author_username, author_name,
            created_at, timestamp_unix, caption, hashtags_json, location_name, location_lat,
            location_lng, tagged_users_json, likes_count, likes_is_truncated,
            reactions_total, reactions_json, album_name, shared_post_json, audience,
            is_archived, is_collab, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ig:AAA111",
            "instagram",
            "AAA111",
            "https://www.instagram.com/p/AAA111/",
            "carousel",
            "my_ig",
            "홍길동",
            "2025-05-10T14:30:00+09:00",
            1746855000,
            "제주도 바다가 정말 예뻤다 #제주도",
            json.dumps(["제주도"]),
            "제주 함덕",
            33.54,
            126.67,
            json.dumps([{"username": "friend_one", "full_name": "김철수"}]),
            12,
            0,
            0,
            "{}",
            None,
            None,
            None,
            0,
            0,
            "merged",
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO posts (
            post_id, platform, shortcode, url, post_type, author_username, author_name,
            created_at, timestamp_unix, caption, hashtags_json, location_name, location_lat,
            location_lng, tagged_users_json, likes_count, likes_is_truncated,
            reactions_total, reactions_json, album_name, shared_post_json, audience,
            is_archived, is_collab, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ig:ARCH99",
            "instagram",
            "ARCH99",
            None,
            "photo",
            "my_ig",
            "홍길동",
            "2024-02-01T10:00:00+09:00",
            1706749200,
            "보관해 둔 생일 사진",
            json.dumps(["생일"]),
            None,
            None,
            None,
            "[]",
            3,
            0,
            0,
            "{}",
            None,
            None,
            None,
            1,
            0,
            "official",
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO posts (
            post_id, platform, shortcode, url, post_type, author_username, author_name,
            created_at, timestamp_unix, caption, hashtags_json, location_name, location_lat,
            location_lng, tagged_users_json, likes_count, likes_is_truncated,
            reactions_total, reactions_json, album_name, shared_post_json, audience,
            is_archived, is_collab, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "fb:post_1",
            "facebook",
            None,
            None,
            "photo",
            "fb_me",
            "홍길동",
            "2024-08-15T18:00:00+09:00",
            1723712400,
            "제주 여행 앨범 업로드 고마워요",
            "[]",
            "서귀포",
            None,
            None,
            "[]",
            0,
            0,
            5,
            json.dumps({"total": 5, "breakdown": {"LIKE": 3, "LOVE": 2}}),
            "여름휴가",
            None,
            "Friends",
            0,
            0,
            "official",
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO posts (
            post_id, platform, shortcode, url, post_type, author_username, author_name,
            created_at, timestamp_unix, caption, hashtags_json, location_name, location_lat,
            location_lng, tagged_users_json, likes_count, likes_is_truncated,
            reactions_total, reactions_json, album_name, shared_post_json, audience,
            is_archived, is_collab, source, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "fb:shared_1",
            "facebook",
            None,
            None,
            "shared",
            "fb_me",
            "홍길동",
            "2023-11-01T12:00:00+09:00",
            1698807600,
            "",
            "[]",
            None,
            None,
            None,
            "[]",
            0,
            0,
            1,
            json.dumps({"total": 1, "by_type": {"LIKE": 1}}),
            None,
            json.dumps(
                {
                    "url": "https://tech.example.kr/post/1",
                    "title": "분산 스토리지 설계 노트",
                    "text": "메타 아카이브 참고 기사",
                    "domain": "tech.example.kr",
                    "thumbnail_local_path": "media/facebook/links/shared_thumb.jpg",
                }
            ),
            "Friends",
            0,
            0,
            "merged",
            "{}",
        ),
    )

    # Media rows
    conn.executemany(
        """
        INSERT INTO media (
            owner_type, owner_id, platform, seq, media_id, media_type,
            local_path, xmp_path, original_uri, width, height, duration_sec,
            has_audio, is_audio_muted, taken_at_exif, gps_lat, gps_lng,
            sha256, byte_size, is_carousel_cover
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "post",
                "ig:AAA111",
                "instagram",
                0,
                "m1",
                "image",
                "media/instagram/posts/jeju.jpg",
                None,
                None,
                1080,
                1080,
                None,
                None,
                None,
                "2025:05:10 14:30:00+09:00",
                None,
                None,
                "sha1",
                34,
                0,
            ),
            (
                "post",
                "ig:AAA111",
                "instagram",
                1,
                "m2",
                "video",
                "media/instagram/posts/jeju_clip.mp4",
                None,
                None,
                1080,
                1920,
                12.5,
                1,
                0,
                None,
                None,
                None,
                "sha2",
                100,
                0,
            ),
            (
                "post",
                "fb:post_1",
                "facebook",
                0,
                "m3",
                "image",
                "media/facebook/posts/album_cover.jpg",
                None,
                None,
                800,
                600,
                None,
                None,
                None,
                None,
                None,
                None,
                "sha3",
                28,
                0,
            ),
            (
                "story",
                "ig_story_1",
                "instagram",
                0,
                "ms1",
                "image",
                "media/instagram/stories/hl_01.jpg",
                None,
                None,
                1080,
                1920,
                None,
                None,
                None,
                None,
                None,
                None,
                "sha4",
                24,
                0,
            ),
            (
                "message",
                "fb/e2ee:thread_1#1",
                "facebook",
                0,
                "mm1",
                "image",
                "media/facebook/messages/dm_photo.jpg",
                None,
                None,
                640,
                480,
                None,
                None,
                None,
                None,
                None,
                None,
                "sha5",
                24,
                0,
            ),
        ],
    )

    # Nested comments (parent depth 0 -> child depth 1)
    conn.executemany(
        """
        INSERT INTO comments (
            comment_id, post_id, parent_comment_id, platform, depth,
            author_username, author_name, text, created_at, timestamp_unix,
            like_count, media_json, stickers_json, reactions_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "c1",
                "ig:AAA111",
                None,
                "instagram",
                0,
                "friend_one",
                "김철수",
                "생일 축하 겸 제주도 멋지다!",
                "2025-05-10T15:00:00+09:00",
                1746856800,
                2,
                "[]",
                "[]",
                None,
            ),
            (
                "c2",
                "ig:AAA111",
                "c1",
                "instagram",
                1,
                "my_ig",
                "홍길동",
                "고마워요 철수야",
                "2025-05-10T15:05:00+09:00",
                1746857100,
                1,
                "[]",
                "[]",
                None,
            ),
        ],
    )

    conn.execute(
        """
        INSERT INTO likes (post_id, platform, user_id, username, full_name, profile_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("ig:AAA111", "instagram", "u_friend", "friend_one", "김철수", None),
    )

    conn.execute(
        """
        INSERT INTO stories (
            story_id, platform, created_at, timestamp_unix, source,
            highlight_id, highlight_title, highlight_cover, location_name, stickers_json, likes_count, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ig_story_1",
            "instagram",
            "2025-05-10T16:00:00+09:00",
            1746860400,
            "merged",
            "hl_jeju",
            "제주기록",
            "media/instagram/stories/hl_01.jpg",
            "애월",
            json.dumps(["바다"]),
            4,
            "{}",
        ),
    )

    conn.executemany(
        """
        INSERT INTO connections (
            platform, relation, user_id, username, full_name,
            profile_url, avatar_local_path, connected_at, timestamp_unix, match_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "instagram",
                "follower",
                "u_friend",
                "friend_one",
                "김철수",
                "https://www.instagram.com/friend_one/",
                None,
                "2024-01-01T00:00:00+09:00",
                1704034800,
                "high",
            ),
            (
                "facebook",
                "friend",
                "fb_u2",
                None,
                "이영희",
                None,
                None,
                "2023-03-01T00:00:00+09:00",
                1677596400,
                "exact",
            ),
        ],
    )

    conn.execute(
        """
        INSERT INTO message_threads (
            thread_id, platform, title, participants_json, is_e2ee, is_pending, message_count, last_timestamp_unix
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "fb/e2ee:thread_1",
            "facebook",
            "김철수와의 비밀채팅",
            json.dumps([{"username": "friend_one", "full_name": "김철수"}]),
            1,
            0,
            2,
            1746864000,
        ),
    )
    conn.executemany(
        """
        INSERT INTO messages (
            thread_id, message_id, platform, seq, sender, timestamp_unix,
            timestamp_ms, created_at, text, reactions_json, reply_to_id, shared_link, is_unsent, call_duration
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "fb/e2ee:thread_1",
                "msg_0",
                "facebook",
                0,
                "김철수",
                1746863000,
                1746863000000,
                "2025-05-10T16:43:20+09:00",
                "제주 사진 보내줄게",
                json.dumps([{"reaction": "❤", "actor": "홍길동"}]),
                None,
                None,
                0,
                None,
            ),
            (
                "fb/e2ee:thread_1",
                "msg_1",
                "facebook",
                1,
                "홍길동",
                1746864000,
                1746864000000,
                "2025-05-10T17:00:00+09:00",
                "사진 고마워요",
                "[]",
                "msg_0",
                None,
                0,
                None,
            ),
        ],
    )

    conn.executemany(
        """
        INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("post", "ig:AAA111", "instagram", "my_ig 홍길동", "제주도 바다가 정말 예뻤다 #제주도", 1746855000),
            ("post", "ig:ARCH99", "instagram", "my_ig 홍길동", "보관해 둔 생일 사진 #생일", 1706749200),
            ("post", "fb:post_1", "facebook", "fb_me 홍길동", "제주 여행 앨범 업로드 고마워요 여름휴가", 1723712400),
            ("post", "fb:shared_1", "facebook", "fb_me 홍길동", "분산 스토리지 설계 노트 메타 아카이브 참고 기사 tech.example.kr", 1698807600),
            ("comment", "ig:AAA111", "instagram", "friend_one 김철수", "생일 축하 겸 제주도 멋지다!", 1746856800),
            ("comment", "ig:AAA111", "instagram", "my_ig 홍길동", "고마워요 철수야", 1746857100),
            ("message", "fb/e2ee:thread_1", "facebook", "김철수", "제주 사진 보내줄게", 1746863000),
            ("story", "ig_story_1", "instagram", "", "제주기록 애월 바다", 1746860400),
            ("connection", "friend_one", "instagram", "friend_one 김철수", "friend_one 김철수", 1704034800),
        ],
    )

    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("VACUUM;")
    conn.commit()
    conn.close()
    return root


@pytest.fixture(scope="module")
def archive_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, TestClient]]:
    archive_dir = _build_fixture_archive(tmp_path_factory.mktemp("archive"))
    with TestClient(create_app(archive_dir)) as client:
        yield archive_dir, client


def test_health_overview_and_ro_mode(archive_client: tuple[Path, TestClient]) -> None:
    archive_dir, client = archive_client

    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["schema_version"] == "1"

    ov = client.get("/api/overview").json()
    assert ov["totals"]["posts"] == 4
    assert ov["totals"]["comments"] == 2
    assert ov["by_platform"]["instagram"]["posts"] == 2
    assert ov["by_platform"]["facebook"]["posts"] == 2

    profiles = client.get("/api/profiles", params={"platform": "instagram"}).json()
    assert len(profiles["items"]) == 1
    assert profiles["items"][0]["username"] == "my_ig"

    # immutable=1 read-only queries must never leave WAL or SHM sidecar files
    for sidecar in ("archive.sqlite-wal", "archive.sqlite-shm", "archive.sqlite-journal"):
        assert not (archive_dir / sidecar).exists()


def test_missing_and_incompatible_schema_errors(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "missing")) as missing_client:
        assert missing_client.get("/api/health").status_code == 503

    bad_dir = _build_fixture_archive(tmp_path / "bad_schema", schema_version="999")
    with TestClient(create_app(bad_dir)) as bad_client:
        assert bad_client.get("/api/health").status_code == 500


def test_posts_nested_comments_likes_stories_and_fb_views(
    archive_client: tuple[Path, TestClient],
) -> None:
    _, client = archive_client

    # Filter archived Instagram posts
    res_arch = client.get("/api/posts", params={"platform": "instagram", "archived": "true"}).json()
    assert res_arch["total"] == 1
    assert res_arch["items"][0]["post_id"] == "ig:ARCH99"

    # Year/month filter on Facebook posts
    res_fb = client.get(
        "/api/posts",
        params={"platform": "facebook", "year": 2024, "month": 8, "album": "여름휴가"},
    ).json()
    assert res_fb["total"] == 1
    assert res_fb["items"][0]["reactions"]["breakdown"]["LOVE"] == 2

    # Post detail reconstructs nested comments + likes
    detail = client.get("/api/posts/ig:AAA111").json()
    assert len(detail["media"]) == 2
    assert len(detail["comments"]) == 1
    assert detail["comments"][0]["author_username"] == "friend_one"
    assert len(detail["comments"][0]["replies"]) == 1
    assert detail["comments"][0]["replies"][0]["text"] == "고마워요 철수야"
    assert detail["likes_users"][0]["username"] == "friend_one"

    # Highlights, stories, connections, albums, timeline
    hls = client.get("/api/highlights").json()["items"]
    assert hls[0]["highlight_id"] == "hl_jeju"
    assert hls[0]["count"] == 1

    stories = client.get("/api/stories", params={"highlight_id": "hl_jeju"}).json()
    assert stories["total"] == 1
    assert stories["items"][0]["media"]["local_path"] == "media/instagram/stories/hl_01.jpg"

    conns = client.get("/api/connections", params={"platform": "instagram", "q": "김철수"}).json()
    assert conns["total"] == 1
    assert conns["items"][0]["username"] == "friend_one"

    albums = client.get("/api/albums").json()["items"]
    assert albums[0]["album_name"] == "여름휴가"

    timeline = client.get("/api/timeline").json()["items"]
    assert timeline == [
        {"year": 2024, "month": 8, "count": 1},
        {"year": 2023, "month": 11, "count": 1},
    ]

    # Facebook original vs shared post_type filtering & shared link card thumbnail serving
    fb_orig = client.get("/api/posts", params={"platform": "facebook", "post_type": "original"}).json()
    assert fb_orig["total"] == 1
    assert fb_orig["items"][0]["post_id"] == "fb:post_1"

    fb_shared = client.get("/api/posts", params={"platform": "facebook", "post_type": "shared"}).json()
    assert fb_shared["total"] == 1
    sp = fb_shared["items"][0]["shared_post"]
    assert sp["domain"] == "tech.example.kr"
    assert sp["title"] == "분산 스토리지 설계 노트"
    assert sp["thumbnail_local_path"] == "media/facebook/links/shared_thumb.jpg"
    assert client.get(f"/media/{sp['thumbnail_local_path']}").status_code == 200

    # Shared link title is searchable via FTS5 trigram
    shared_search = client.get("/api/search", params={"q": "스토리지", "platform": "facebook"}).json()
    assert shared_search["mode"] == "fts5_trigram"
    assert any(item["entity_id"] == "fb:shared_1" for item in shared_search["items"])


def test_threads_e2ee_message_media_and_person_activity(
    archive_client: tuple[Path, TestClient],
) -> None:
    _, client = archive_client

    threads = client.get("/api/threads", params={"platform": "facebook"}).json()
    assert threads["total"] == 1
    assert threads["items"][0]["is_e2ee"] is True

    msgs = client.get("/api/threads/fb/e2ee:thread_1/messages").json()
    assert msgs["total"] == 2
    assert msgs["items"][1]["seq"] == 1
    assert len(msgs["items"][1]["media"]) == 1
    assert msgs["items"][1]["media"][0]["local_path"] == "media/facebook/messages/dm_photo.jpg"

    # Person activity lookup expands username <-> full_name via connections
    person = client.get("/api/people/friend_one").json()
    assert len(person["connections"]) == 1
    assert len(person["comments"]) == 1
    assert len(person["liked_posts"]) == 1
    assert len(person["messages"]) == 1
    assert person["messages"][0]["thread_id"] == "fb/e2ee:thread_1"


def test_hybrid_korean_search_short_and_trigram(
    archive_client: tuple[Path, TestClient],
) -> None:
    _, client = archive_client

    # 2-char Korean fallback via instr()
    short_jeju = client.get("/api/search", params={"q": "제주"}).json()
    assert short_jeju["mode"] == "instr_short"
    assert short_jeju["total"] >= 4

    short_bday = client.get("/api/search", params={"q": "생일", "entity_type": "comment"}).json()
    assert short_bday["mode"] == "instr_short"
    assert short_bday["total"] == 1

    # 3+ char Korean via FTS5 trigram
    tri = client.get("/api/search", params={"q": "고마워요"}).json()
    assert tri["mode"] == "fts5_trigram"
    assert tri["total"] == 2

    tri_ig = client.get("/api/search", params={"q": "제주도", "platform": "instagram"}).json()
    assert tri_ig["mode"] == "fts5_trigram"
    assert tri_ig["total"] >= 2


def test_media_range_streaming_and_path_traversal_guards(
    archive_client: tuple[Path, TestClient],
) -> None:
    _, client = archive_client

    # Full GET and HEAD
    full_res = client.get("/media/instagram/posts/jeju_clip.mp4")
    assert full_res.status_code == 200
    assert full_res.headers["accept-ranges"] == "bytes"
    assert len(full_res.content) == 100

    head_res = client.head("/media/media/instagram/posts/jeju_clip.mp4")
    assert head_res.status_code == 200
    assert head_res.headers["content-length"] == "100"
    assert head_res.content == b""

    # HTTP Range 206 partial responses
    r1 = client.get("/media/instagram/posts/jeju_clip.mp4", headers={"Range": "bytes=0-9"})
    assert r1.status_code == 206
    assert r1.headers["content-range"] == "bytes 0-9/100"
    assert r1.content == bytes(range(0, 10))

    r2 = client.get("/media/instagram/posts/jeju_clip.mp4", headers={"Range": "bytes=90-"})
    assert r2.status_code == 206
    assert r2.headers["content-range"] == "bytes 90-99/100"
    assert r2.content == bytes(range(90, 100))

    r3 = client.get("/media/instagram/posts/jeju_clip.mp4", headers={"Range": "bytes=-15"})
    assert r3.status_code == 206
    assert r3.headers["content-range"] == "bytes 85-99/100"
    assert r3.content == bytes(range(85, 100))

    # Out-of-bounds Range -> 416
    r_bad = client.get("/media/instagram/posts/jeju_clip.mp4", headers={"Range": "bytes=500-600"})
    assert r_bad.status_code == 416

    # Path traversal and non-media files must NEVER succeed
    for bad_path in (
        "/media/../archive.sqlite",
        "/media/%2e%2e/archive.sqlite",
        "/media/archive.sqlite",
        "/media/..\\archive.sqlite",
        "/media/C:/Windows/win.ini",
    ):
        resp = client.get(bad_path)
        assert resp.status_code in (400, 404), f"Unexpected {resp.status_code} for {bad_path}"


def test_static_spa_serves_and_has_zero_external_cdns(
    archive_client: tuple[Path, TestClient],
) -> None:
    _, client = archive_client

    idx = client.get("/")
    assert idx.status_code == 200
    assert "text/html" in idx.headers["content-type"]

    app_js = client.get("/static/js/app.js")
    assert app_js.status_code == 200
    assert app_js.headers.get("cache-control") == "no-cache"

    font_res = client.get("/static/fonts/PretendardVariable.woff2")
    assert font_res.status_code == 200
    assert "immutable" in (font_res.headers.get("cache-control") or "")
    assert font_res.content[:4] == b"wOF2"

    media_res = client.get("/media/instagram/posts/jeju.jpg")
    assert media_res.status_code == 200
    assert "immutable" in (media_res.headers.get("cache-control") or "")
    assert "etag" in media_res.headers

    static_dir = Path(__file__).resolve().parents[1] / "src" / "meta_archive_viewer" / "static"
    for asset in static_dir.rglob("*"):
        if asset.is_file() and asset.suffix in {".html", ".css", ".js"}:
            text = asset.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text, f"External URL found in {asset}"


def test_avif_media_mime_sniffs_disguised_jpeg_and_true_avif(
    archive_client: tuple[Path, TestClient],
) -> None:
    archive_dir, client = archive_client
    fb_posts = archive_dir / "media" / "facebook" / "posts"
    (fb_posts / "disguised.avif").write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 20)
    (fb_posts / "real.avif").write_bytes(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 16)

    r_jpeg = client.get("/media/facebook/posts/disguised.avif")
    assert r_jpeg.status_code == 200
    assert r_jpeg.headers["content-type"] == "image/jpeg"

    r_avif = client.get("/media/facebook/posts/real.avif")
    assert r_avif.status_code == 200
    assert r_avif.headers["content-type"] == "image/avif"


def test_extensionless_magic_mime_activities_tail_messages_group_posts_and_js_fixes(
    tmp_path: Path,
) -> None:
    from meta_archive_viewer.app import _guess_media_mime

    # 1. Pure magic-byte MIME sniffing for extensionless files (`<no_ext>`)
    noext_jpg = tmp_path / "noext_photo"
    noext_jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 16)
    assert _guess_media_mime(noext_jpg) == "image/jpeg"

    noext_png = tmp_path / "noext_png"
    noext_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"P" * 16)
    assert _guess_media_mime(noext_png) == "image/png"

    noext_pdf = tmp_path / "204712957226567"
    noext_pdf.write_bytes(b"%PDF-1.3\nhello")
    assert _guess_media_mime(noext_pdf) == "application/pdf"

    noext_mp4 = tmp_path / "noext_clip"
    noext_mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16)
    assert _guess_media_mime(noext_mp4) == "video/mp4"

    # 2. Build upgraded schema archive with activities, group_post, story caption, and 5 messages
    arch = tmp_path / "arch_v2"
    (arch / "media" / "facebook" / "stickers").mkdir(parents=True, exist_ok=True)
    (arch / "media" / "facebook" / "stickers" / "st1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"S" * 16)

    db_path = arch / "archive.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA_DDL)
    conn.execute("ALTER TABLE posts ADD COLUMN activity_title TEXT")
    conn.execute("ALTER TABLE posts ADD COLUMN group_name TEXT")
    conn.execute("ALTER TABLE stories ADD COLUMN caption TEXT")
    conn.execute("ALTER TABLE message_threads ADD COLUMN avatar_local_path TEXT")
    conn.execute("ALTER TABLE messages ADD COLUMN share_text TEXT")
    conn.execute("ALTER TABLE messages ADD COLUMN share_owner TEXT")
    conn.execute("ALTER TABLE messages ADD COLUMN is_missed_call INTEGER")
    conn.executescript(
        """
        CREATE TABLE activities (
            activity_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT,
            timestamp_unix INTEGER,
            text TEXT,
            title TEXT,
            target_author TEXT,
            group_name TEXT,
            url TEXT,
            source TEXT,
            raw_json TEXT
        );
        """
    )
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
    conn.execute(
        """
        INSERT INTO posts (post_id, platform, post_type, timestamp_unix, caption, activity_title, group_name, is_archived)
        VALUES ('fb:grp_1', 'facebook', 'group_post', 1700000000, '학회 공지', '윤기님이 학회에 글을 남겼습니다.', '컴공학회', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO stories (story_id, platform, timestamp_unix, caption, source)
        VALUES ('fb_st_1', 'facebook', 1700000100, '보관 스토리 캡션', 'official_export')
        """
    )
    conn.execute(
        """
        INSERT INTO message_threads (thread_id, platform, title, avatar_local_path, is_e2ee, is_pending, message_count, last_timestamp_unix)
        VALUES ('ig/grp_1', 'instagram', '단톡방', 'media/instagram/avatars/grp.jpg', 0, 0, 5, 1700000500)
        """
    )
    for seq in range(5):
        ts = 1700000100 + seq * 100
        conn.execute(
            """
            INSERT INTO messages (
                thread_id, message_id, platform, seq, sender, timestamp_unix, timestamp_ms,
                text, reactions_json, share_text, share_owner, is_unsent, is_missed_call
            ) VALUES (?, ?, 'instagram', ?, '윤기', ?, ?, ?, '[]', ?, ?, 0, ?)
            """,
            (
                "ig/grp_1",
                f"m_{seq}",
                seq,
                ts,
                ts * 1000,
                f"메시지 {seq}",
                "공유 릴스 설명" if seq == 4 else None,
                "creator_x" if seq == 4 else None,
                1 if seq == 4 else 0,
            ),
        )
    conn.execute(
        """
        INSERT INTO activities (activity_id, platform, kind, timestamp_unix, text, title, target_author, group_name, url, source)
        VALUES ('fb_cmt_1', 'facebook', 'own_comment', 1700000900, '멋진 글이네요!', '윤기님이 댓글을 남겼습니다.', '김철수', NULL, NULL, 'official_export')
        """
    )
    conn.execute(
        """
        INSERT INTO media (owner_type, owner_id, platform, seq, media_type, local_path, is_carousel_cover)
        VALUES ('activity', 'fb_cmt_1', 'facebook', 0, 'image', 'media/facebook/stickers/st1.png', 0)
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(arch)) as client:
        ov = client.get("/api/overview").json()
        assert ov["totals"]["activities"] == 1

        # Group post filter & metadata
        grp_res = client.get("/api/posts", params={"platform": "facebook", "post_type": "group_post"}).json()
        assert grp_res["total"] == 1
        assert grp_res["items"][0]["group_name"] == "컴공학회"
        assert grp_res["items"][0]["activity_title"] == "윤기님이 학회에 글을 남겼습니다."

        orig_res = client.get("/api/posts", params={"platform": "facebook", "post_type": "original"}).json()
        assert orig_res["total"] == 0

        # Story by story_id + caption
        st_res = client.get("/api/stories", params={"platform": "facebook", "story_id": "fb_st_1"}).json()
        assert st_res["total"] == 1
        assert st_res["items"][0]["caption"] == "보관 스토리 캡션"

        # Tail pagination & before_seq & around_ts on messages
        tail_res = client.get("/api/threads/ig/grp_1/messages", params={"tail": "true", "limit": 2}).json()
        assert tail_res["thread"]["avatar_local_path"] == "media/instagram/avatars/grp.jpg"
        assert [m["seq"] for m in tail_res["items"]] == [3, 4]
        assert tail_res["has_older"] is True
        assert tail_res["items"][1]["share_text"] == "공유 릴스 설명"
        assert tail_res["items"][1]["share_owner"] == "creator_x"
        assert tail_res["items"][1]["is_missed_call"] is True

        older_res = client.get("/api/threads/ig/grp_1/messages", params={"before_seq": 3, "limit": 2}).json()
        assert [m["seq"] for m in older_res["items"]] == [1, 2]
        assert older_res["has_older"] is True

        around_res = client.get(
            "/api/threads/ig/grp_1/messages", params={"around_ts": 1700000300, "limit": 3}
        ).json()
        assert 2 in [m["seq"] for m in around_res["items"]]

        # Activities endpoint
        act_res = client.get("/api/activities", params={"platform": "facebook", "kind": "own_comment"}).json()
        assert act_res["total"] == 1
        assert act_res["items"][0]["text"] == "멋진 글이네요!"
        assert act_res["items"][0]["media"][0]["local_path"] == "media/facebook/stickers/st1.png"

    # 3. Frontend JS regression guards
    js_dir = Path(__file__).resolve().parents[1] / "src" / "meta_archive_viewer" / "static" / "js"
    messages_js = (js_dir / "views" / "messages.js").read_text(encoding="utf-8")
    assert "r.reaction" in messages_js
    dom_js = (js_dir / "dom.js").read_text(encoding="utf-8")
    assert 'media.media_type === "audio"' in dom_js


def test_avatar_enrichment_thread_sorting_preview_in_thread_search_and_media_only(
    tmp_path: Path,
) -> None:
    archive_dir = _build_fixture_archive(tmp_path / "avatar_enrich")
    (archive_dir / "media" / "instagram" / "avatars" / "friend_one.jpg").write_bytes(
        b"\xff\xd8\xff\xe0" + b"F" * 16
    )
    conn = sqlite3.connect(archive_dir / "archive.sqlite")
    conn.execute(
        "UPDATE connections SET avatar_local_path = ? WHERE username = ?",
        ("media/instagram/avatars/friend_one.jpg", "friend_one"),
    )
    conn.execute(
        """
        INSERT INTO profiles (platform, user_id, username, full_name, bio, avatar_local_path, counts_json, exported_at)
        VALUES ('facebook', 'fb_u1', 'fb_me', '홍길동', '', NULL, '{}', '2026-11-20T10:00:00+09:00')
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(archive_dir)) as client:
        # 1. Post list and detail include resolved author/comment/like avatars
        posts = client.get("/api/posts", params={"platform": "instagram"}).json()
        assert posts["items"][0]["author_avatar_local_path"] == "media/instagram/avatars/me.jpg"

        detail = client.get("/api/posts/ig:AAA111").json()
        assert detail["author_avatar_local_path"] == "media/instagram/avatars/me.jpg"
        assert detail["comments"][0]["author_avatar_local_path"] == "media/instagram/avatars/friend_one.jpg"
        assert detail["likes_users"][0]["avatar_local_path"] == "media/instagram/avatars/friend_one.jpg"

        # 2. Thread list resolves partner avatar, last_message_preview, and sort=messages
        threads = client.get("/api/threads", params={"sort": "messages"}).json()
        assert threads["total"] == 1
        fb_thread = threads["items"][0]
        assert fb_thread["thread_id"] == "fb/e2ee:thread_1"
        assert fb_thread["avatar_local_path"] == "media/instagram/avatars/friend_one.jpg"
        assert "사진" in fb_thread["last_message_preview"]

        # 3. Thread messages include owner_names, sender_avatar_local_path, in-thread q, and media_only
        msgs = client.get("/api/threads/fb/e2ee:thread_1/messages").json()
        assert "홍길동" in msgs["thread"]["owner_names"]
        assert "fb_me" in msgs["thread"]["owner_names"]
        partner_msg = next(m for m in msgs["items"] if m["sender"] == "김철수")
        assert partner_msg["sender_avatar_local_path"] == "media/instagram/avatars/friend_one.jpg"

        q_res = client.get("/api/threads/fb/e2ee:thread_1/messages", params={"q": "제주"}).json()
        assert q_res["total"] == 1
        assert all("제주" in (m["text"] + " " + (m["sender"] or "")) for m in q_res["items"])

        media_res = client.get("/api/threads/fb/e2ee:thread_1/messages", params={"media_only": "true"}).json()
        assert media_res["total"] == 1
        assert all(len(m["media"]) > 0 for m in media_res["items"])

        # 4. Person activity includes resolved avatar_local_path
        person = client.get("/api/people/friend_one").json()
        assert person["avatar_local_path"] == "media/instagram/avatars/friend_one.jpg"


def test_memories_on_this_day_yearbook_inner_circle_and_photo_wall(tmp_path: Path) -> None:
    archive_dir = _build_fixture_archive(tmp_path / "memories_api")
    with TestClient(create_app(archive_dir)) as client:
        # 1. On This Day (May 10 matches ig:AAA111 in KST)
        otd = client.get("/api/memories/on-this-day", params={"month": 5, "day": 10}).json()
        assert otd["month"] == 5 and otd["day"] == 10
        assert any(p["post_id"] == "ig:AAA111" for p in otd["posts"])
        assert 2025 in otd["years_active"]

        rand_otd = client.get("/api/memories/on-this-day", params={"random_day": "true"}).json()
        assert 1 <= rand_otd["month"] <= 12
        assert 1 <= rand_otd["day"] <= 31

        # 2. Yearbook (2025 chapter)
        yb = client.get("/api/memories/yearbook", params={"year": 2025}).json()
        assert yb["selected_year"] == 2025
        assert any(y["year"] == 2025 for y in yb["years"])
        assert len(yb["photos"]) >= 1
        assert any(pl["name"] == "제주 함덕" for pl in yb["places"])
        assert any(c["identifier"] == "friend_one" for c in yb["top_commenters"])

        # 3. Inner Circle (cross-platform unified people & groups)
        ic = client.get("/api/memories/people", params={"limit": 10}).json()
        assert len(ic["people"]) >= 1
        assert any(p["display_name"] == "김철수" for p in ic["people"])

        # 4. Photo Wall + place filter + media_ids keepsake filter
        wall = client.get("/api/memories/photos", params={"limit": 20}).json()
        assert wall["total"] >= 3
        assert any(pl["name"] == "제주 함덕" for pl in wall["places"])

        by_place = client.get("/api/memories/photos", params={"place": "제주 함덕"}).json()
        assert by_place["total"] >= 1
        assert all(item["location_name"] == "제주 함덕" for item in by_place["items"])

        first_id = wall["items"][0]["id"]
        favs = client.get("/api/memories/photos", params={"media_ids": str(first_id)}).json()
        assert favs["total"] == 1
        assert favs["items"][0]["id"] == first_id

        empty_favs = client.get("/api/memories/photos", params={"media_ids": ""}).json()
        assert empty_favs["total"] == 0

    # Verify static Memories SPA assets exist and avoid innerHTML
    static_dir = Path(__file__).resolve().parents[1] / "src" / "meta_archive_viewer" / "static"
    memories_js = (static_dir / "js" / "views" / "memories.js").read_text(encoding="utf-8")
    assert "innerHTML" not in memories_js
    assert "renderMemoriesView" in memories_js


