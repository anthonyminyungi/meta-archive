"""Tests for SQLite FTS5 trigram indexer, read-only immutable access, hybrid Korean search, and SHA-256 manifest."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from meta_archive_pipeline.build.indexer import (
    build_sqlite_index,
    open_readonly_db,
    search_archive,
    verify_manifest_sha256,
    write_manifest_sha256,
)
from meta_archive_pipeline.build.merger import materialize_archive, merge_all
from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    Location,
    MediaItem,
    Message,
    MessageThread,
    Post,
    Profile,
    ProfileCounts,
    SharedPostRef,
    Story,
    UserRef,
)
from meta_archive_pipeline.schema.paths import ARCHIVE_SCHEMA_VERSION, ArchivePaths


def _build_full_sample_archive(tmp_path: Path) -> Path:
    src_root = tmp_path / "source"
    img = src_root / "media" / "posts" / "jeju.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 24), color=(0, 128, 255)).save(img, format="JPEG")

    ts = 1700000000
    bundle = OfficialExportBundle(
        platform="instagram",
        root_dir=src_root,
        profile=Profile(
            platform="instagram",
            username="yungis_",
            full_name="윤기",
            counts=ProfileCounts(posts=1, followers=1),
        ),
        posts=[
            Post(
                platform="instagram",
                post_id="p_jeju",
                shortcode="JEJU1",
                timestamp_unix=ts,
                caption="협재해수욕장 첫날 기록 #제주도",
                hashtags=["제주도"],
                location=Location(name="협재해수욕장"),
                author=UserRef(username="yungis_", full_name="윤기"),
                media=[MediaItem(media_type="image", local_path="media/posts/jeju.jpg")],
                likes=LikesInfo(count=1, users=[UserRef(username="alice", full_name="앨리스")]),
                comments=[
                    Comment(
                        comment_id="c1",
                        author=UserRef(username="alice", full_name="앨리스"),
                        text="바다 색깔 정말 예쁘다!",
                        timestamp_unix=ts + 60,
                        replies=[
                            Comment(
                                comment_id="r1",
                                author=UserRef(username="yungis_", full_name="윤기"),
                                text="고마워 조심히 와!",
                                timestamp_unix=ts + 120,
                            )
                        ],
                    )
                ],
                source="merged",
            ),
            Post(
                platform="facebook",
                post_id="fb_shared_1",
                post_type="shared",
                timestamp_unix=ts + 100,
                caption="",
                shared_post=SharedPostRef(
                    url="https://tech.example.kr/post/9",
                    title="분산 스토리지 설계 노트",
                    text="메타 아카이브 참고 문서",
                    domain="tech.example.kr",
                ),
                source="official_export",
            ),
        ],
        stories=[
            Story(
                platform="instagram",
                story_id="st1",
                timestamp_unix=ts + 200,
                highlight_title="겨울여행",
                media=None,
            )
        ],
        connections=[
            Connection(
                platform="instagram",
                relation="follower",
                username="alice",
                full_name="앨리스",
                timestamp_unix=ts - 1000,
            )
        ],
        messages=[
            MessageThread(
                platform="instagram",
                thread_id="inbox/alice",
                title="앨리스",
                participants=[UserRef(full_name="윤기"), UserRef(full_name="앨리스")],
                messages=[
                    Message(
                        sender="앨리스",
                        timestamp_unix=ts + 300,
                        timestamp_ms=(ts + 300) * 1000,
                        text="생일 축하해! 다음엔 부산 가자",
                    )
                ],
            )
        ],
    )

    out_dir = tmp_path / "archive"
    merged = merge_all(official_bundles=[bundle])
    materialize_archive(merged, out_dir, source_roots=[src_root], tz_offset="+09:00")
    return out_dir


def test_indexer_readonly_hybrid_korean_search_and_manifest(tmp_path: Path) -> None:
    archive_dir = _build_full_sample_archive(tmp_path)
    idx_res = build_sqlite_index(archive_dir)
    manifest_path, file_count = write_manifest_sha256(archive_dir)

    paths = ArchivePaths(archive_dir)
    assert idx_res.db_path == paths.db_path
    assert manifest_path == paths.manifest_path
    assert file_count >= 8  # 5 data json + ambiguous report + image + xmp + sqlite
    assert idx_res.counts["posts"] == 2
    assert idx_res.counts["comments"] == 2
    assert idx_res.counts["likes"] == 1
    assert idx_res.counts["messages"] == 1

    # No WAL/SHM side files remain
    assert not Path(f"{paths.db_path}-wal").exists()
    assert not Path(f"{paths.db_path}-shm").exists()

    # Open strictly read-only with mode=ro&immutable=1
    conn = open_readonly_db(paths.db_path)
    try:
        jmode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(jmode).lower() == "delete"

        schema_ver = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert schema_ver == str(ARCHIVE_SCHEMA_VERSION)

        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO schema_meta (key, value) VALUES ('x', 'y')")

        # 3+ char Korean queries via FTS5 trigram
        r3_post = search_archive(conn, "제주도")
        assert any(r["entity_type"] == "post" for r in r3_post)

        r3_tag = search_archive(conn, "#제주도")
        assert any(r["entity_type"] == "post" for r in r3_tag)

        r3_loc = search_archive(conn, "해수욕장")
        assert any(r["entity_type"] == "post" for r in r3_loc)

        r3_reply = search_archive(conn, "고마워")
        assert any(r["entity_type"] == "comment" for r in r3_reply)

        r3_shared = search_archive(conn, "스토리지")
        assert any(r["entity_id"] == "fb_shared_1" for r in r3_shared)

        r2_shared = search_archive(conn, "노트")
        assert any(r["entity_id"] == "fb_shared_1" for r in r2_shared)

        r3_story = search_archive(conn, "겨울여행")
        assert any(r["entity_type"] == "story" for r in r3_story)

        # 1~2 char Korean queries via INSTR fallback
        r2_jeju = search_archive(conn, "제주")
        assert any(r["entity_type"] == "post" for r in r2_jeju)

        r2_sea = search_archive(conn, "바다")
        assert any(r["entity_type"] == "comment" for r in r2_sea)

        r2_bday = search_archive(conn, "생일")
        assert any(r["entity_type"] == "message" for r in r2_bday)

        r2_author = search_archive(conn, "윤기")
        assert len(r2_author) >= 2

        r1_char = search_archive(conn, "부산")
        assert any(r["entity_type"] == "message" for r in r1_char)
        assert search_archive(conn, "   ") == []
    finally:
        conn.close()

    # Manifest verification passes cleanly, and detects 1-byte tampering
    checked, failures = verify_manifest_sha256(archive_dir)
    assert checked == file_count
    assert failures == []

    media_file = archive_dir / "media" / "instagram" / "posts" / "jeju.jpg"
    data = bytearray(media_file.read_bytes())
    data[0] ^= 0xFF
    media_file.write_bytes(bytes(data))

    checked_after, failures_after = verify_manifest_sha256(archive_dir)
    assert checked_after == file_count
    assert len(failures_after) == 1
    assert "jeju.jpg" in failures_after[0]
