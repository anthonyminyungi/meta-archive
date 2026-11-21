"""SQLite FTS5 trigram read-only indexer, hybrid Korean search, and SHA-256 manifest tools."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meta_archive_pipeline import __version__
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    MediaItem,
    MessageThread,
    Post,
    Profile,
    Story,
)
from meta_archive_pipeline.schema.paths import (
    ARCHIVE_SCHEMA_VERSION,
    ArchivePaths,
    normalize_rel_path,
)
from meta_archive_pipeline.utils.fs import atomic_write_bytes, sha256_file, to_long_path


@dataclass(frozen=True)
class IndexBuildResult:
    """Result summary from building archive.sqlite (and optional manifest)."""

    db_path: Path
    manifest_path: Path | None
    counts: dict[str, int]
    manifest_files: int = 0


_DDL = """
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

CREATE INDEX idx_posts_platform_ts ON posts(platform, timestamp_unix DESC);
CREATE INDEX idx_media_owner ON media(owner_type, owner_id, seq);
CREATE INDEX idx_comments_post ON comments(post_id, timestamp_unix);
CREATE INDEX idx_comments_author ON comments(author_username, author_name);
CREATE INDEX idx_likes_post ON likes(post_id);
CREATE INDEX idx_likes_user ON likes(username, full_name);
CREATE INDEX idx_stories_platform_ts ON stories(platform, timestamp_unix DESC);
CREATE INDEX idx_connections_rel ON connections(platform, relation, username);
CREATE INDEX idx_messages_thread_ts ON messages(thread_id, timestamp_ms);
CREATE INDEX idx_messages_sender ON messages(sender);

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


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    long_p = to_long_path(path)
    if not os.path.exists(long_p):
        return []
    with open(long_p, "rb") as handle:
        data = json.loads(handle.read().decode("utf-8"))
    return data if isinstance(data, list) else []


def _insert_media_rows(
    conn: sqlite3.Connection,
    owner_type: str,
    owner_id: str,
    platform: str,
    items: Sequence[MediaItem],
) -> int:
    count = 0
    for seq, m in enumerate(items):
        conn.execute(
            """
            INSERT INTO media (
                owner_type, owner_id, platform, seq, media_id, media_type,
                local_path, xmp_path, original_uri, width, height, duration_sec,
                has_audio, is_audio_muted, taken_at_exif, gps_lat, gps_lng,
                sha256, byte_size, is_carousel_cover
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_type,
                owner_id,
                platform,
                seq,
                m.media_id,
                m.media_type,
                m.local_path,
                m.xmp_path,
                m.original_uri,
                m.width,
                m.height,
                m.duration_sec,
                int(m.has_audio) if m.has_audio is not None else None,
                int(m.is_audio_muted) if m.is_audio_muted is not None else None,
                m.taken_at_exif,
                m.gps_lat,
                m.gps_lng,
                m.sha256,
                m.byte_size,
                int(m.is_carousel_cover),
            ),
        )
        count += 1
    return count


def _insert_comments_recursive(
    conn: sqlite3.Connection,
    post_id: str,
    platform: str,
    comments: Sequence[Comment],
    parent_comment_id: str | None = None,
    depth: int = 0,
) -> int:
    inserted = 0
    for c in comments:
        conn.execute(
            """
            INSERT INTO comments (
                comment_id, post_id, parent_comment_id, platform, depth,
                author_username, author_name, text, created_at, timestamp_unix,
                like_count, media_json, stickers_json, reactions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.comment_id,
                post_id,
                parent_comment_id,
                platform,
                depth,
                c.author.username,
                c.author.full_name,
                c.text,
                c.created_at.isoformat() if c.created_at else None,
                c.timestamp_unix,
                c.like_count,
                json.dumps([m.model_dump(mode="json") for m in c.media], ensure_ascii=False),
                json.dumps(c.stickers, ensure_ascii=False),
                c.reactions.model_dump_json() if c.reactions else None,
            ),
        )
        inserted += 1

        author_str = " ".join(
            part for part in (c.author.username, c.author.full_name) if part
        )
        if c.text or author_str:
            conn.execute(
                """
                INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("comment", post_id, platform, author_str, c.text, c.timestamp_unix or 0),
            )

        if c.replies:
            inserted += _insert_comments_recursive(
                conn,
                post_id,
                platform,
                c.replies,
                parent_comment_id=c.comment_id,
                depth=depth + 1,
            )
    return inserted


def build_sqlite_index(archive_root: Path) -> IndexBuildResult:
    """Build archive.sqlite (normalized tables + FTS5 trigram + DELETE/VACUUM) from archive/data/."""
    paths = ArchivePaths(archive_root)
    os.makedirs(to_long_path(paths.root), exist_ok=True)

    profiles = [Profile.model_validate(d) for d in _load_json_list(paths.profiles_json)]
    posts = [Post.model_validate(d) for d in _load_json_list(paths.posts_json)]
    stories = [Story.model_validate(d) for d in _load_json_list(paths.stories_json)]
    connections = [Connection.model_validate(d) for d in _load_json_list(paths.connections_json)]
    threads = [MessageThread.model_validate(d) for d in _load_json_list(paths.messages_json)]

    counts = {
        "profiles": 0,
        "posts": 0,
        "media": 0,
        "comments": 0,
        "likes": 0,
        "stories": 0,
        "connections": 0,
        "message_threads": 0,
        "messages": 0,
    }

    tmp_path: str | None = None
    conn: sqlite3.Connection | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=to_long_path(paths.root),
            prefix=".archive.",
            suffix=".sqlite.tmp",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name

        conn = sqlite3.connect(tmp_path)
        conn.executescript(_DDL)

        conn.executemany(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", str(ARCHIVE_SCHEMA_VERSION)),
                ("built_at", datetime.now(timezone.utc).isoformat()),
                ("pipeline_version", __version__),
            ],
        )

        for prof in profiles:
            conn.execute(
                """
                INSERT INTO profiles (
                    platform, user_id, username, full_name, bio,
                    avatar_local_path, counts_json, exported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prof.platform,
                    prof.user_id,
                    prof.username,
                    prof.full_name,
                    prof.bio,
                    prof.avatar_local_path,
                    prof.counts.model_dump_json(),
                    prof.exported_at.isoformat() if prof.exported_at else None,
                ),
            )
            counts["profiles"] += 1

        for post in posts:
            loc_name = post.location.name if post.location else None
            loc_lat = post.location.lat if post.location else None
            loc_lng = post.location.lng if post.location else None
            conn.execute(
                """
                INSERT OR REPLACE INTO posts (
                    post_id, platform, shortcode, url, post_type,
                    author_username, author_name, created_at, timestamp_unix,
                    caption, hashtags_json, location_name, location_lat, location_lng,
                    tagged_users_json, likes_count, likes_is_truncated,
                    reactions_total, reactions_json, album_name, shared_post_json,
                    audience, is_archived, is_collab, source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.post_id,
                    post.platform,
                    post.shortcode,
                    post.url,
                    post.post_type,
                    post.author.username,
                    post.author.full_name,
                    post.created_at.isoformat() if post.created_at else None,
                    post.timestamp_unix,
                    post.caption,
                    json.dumps(post.hashtags, ensure_ascii=False),
                    loc_name,
                    loc_lat,
                    loc_lng,
                    json.dumps(
                        [u.model_dump(mode="json") for u in post.tagged_users],
                        ensure_ascii=False,
                    ),
                    post.likes.count,
                    int(post.likes.is_truncated),
                    post.reactions.total,
                    post.reactions.model_dump_json(),
                    post.album_name,
                    post.shared_post.model_dump_json() if post.shared_post else None,
                    post.audience,
                    int(post.is_archived),
                    int(post.is_collab),
                    post.source,
                    post.model_dump_json(),
                ),
            )
            counts["posts"] += 1
            counts["media"] += _insert_media_rows(
                conn, "post", post.post_id, post.platform, post.media
            )
            counts["comments"] += _insert_comments_recursive(
                conn, post.post_id, post.platform, post.comments
            )

            for u in post.likes.users:
                conn.execute(
                    """
                    INSERT INTO likes (post_id, platform, user_id, username, full_name, profile_url)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (post.post_id, post.platform, u.user_id, u.username, u.full_name, u.profile_url),
                )
                counts["likes"] += 1

            post_author = " ".join(
                p for p in (post.author.username, post.author.full_name) if p
            )
            shared_parts = (
                (
                    post.shared_post.title,
                    post.shared_post.text,
                    post.shared_post.author_name,
                    post.shared_post.domain,
                    post.shared_post.url,
                )
                if post.shared_post
                else ()
            )
            post_body = " ".join(
                p
                for p in (
                    post.caption,
                    loc_name,
                    post.album_name,
                    " ".join(f"#{t}" for t in post.hashtags),
                    *shared_parts,
                )
                if p
            )
            if post_body or post_author:
                conn.execute(
                    """
                    INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("post", post.post_id, post.platform, post_author, post_body, post.timestamp_unix or 0),
                )

        for story in stories:
            loc_name = story.location.name if story.location else None
            conn.execute(
                """
                INSERT OR REPLACE INTO stories (
                    story_id, platform, created_at, timestamp_unix, source,
                    highlight_id, highlight_title, highlight_cover,
                    location_name, stickers_json, likes_count, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    story.story_id,
                    story.platform,
                    story.created_at.isoformat() if story.created_at else None,
                    story.timestamp_unix,
                    story.source,
                    story.highlight_id,
                    story.highlight_title,
                    story.highlight_cover,
                    loc_name,
                    json.dumps(story.stickers, ensure_ascii=False),
                    story.likes_count,
                    story.model_dump_json(),
                ),
            )
            counts["stories"] += 1
            if story.media is not None:
                counts["media"] += _insert_media_rows(
                    conn, "story", story.story_id, story.platform, [story.media]
                )
            story_body = " ".join(
                p for p in (story.highlight_title, loc_name, *story.stickers) if p
            )
            if story_body:
                conn.execute(
                    """
                    INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("story", story.story_id, story.platform, "", story_body, story.timestamp_unix or 0),
                )

        for c in connections:
            conn.execute(
                """
                INSERT INTO connections (
                    platform, relation, user_id, username, full_name,
                    profile_url, avatar_local_path, connected_at,
                    timestamp_unix, match_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    c.platform,
                    c.relation,
                    c.user_id,
                    c.username,
                    c.full_name,
                    c.profile_url,
                    c.avatar_local_path,
                    c.connected_at.isoformat() if c.connected_at else None,
                    c.timestamp_unix,
                    c.match_confidence,
                ),
            )
            counts["connections"] += 1
            conn_author = " ".join(p for p in (c.username, c.full_name) if p)
            if conn_author:
                conn.execute(
                    """
                    INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "connection",
                        c.username or c.full_name or "",
                        c.platform,
                        conn_author,
                        conn_author,
                        c.timestamp_unix or 0,
                    ),
                )

        for thread in threads:
            last_ts = max((m.timestamp_unix or 0 for m in thread.messages), default=0)
            conn.execute(
                """
                INSERT OR REPLACE INTO message_threads (
                    thread_id, platform, title, participants_json,
                    is_e2ee, is_pending, message_count, last_timestamp_unix
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread.thread_id,
                    thread.platform,
                    thread.title,
                    json.dumps(
                        [p.model_dump(mode="json") for p in thread.participants],
                        ensure_ascii=False,
                    ),
                    int(thread.is_e2ee),
                    int(thread.is_pending),
                    len(thread.messages),
                    last_ts or None,
                ),
            )
            counts["message_threads"] += 1

            for seq, msg in enumerate(thread.messages):
                conn.execute(
                    """
                    INSERT INTO messages (
                        thread_id, message_id, platform, seq, sender,
                        timestamp_unix, timestamp_ms, created_at, text,
                        reactions_json, reply_to_id, shared_link, is_unsent, call_duration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread.thread_id,
                        msg.message_id,
                        thread.platform,
                        seq,
                        msg.sender,
                        msg.timestamp_unix,
                        msg.timestamp_ms,
                        msg.created_at.isoformat() if msg.created_at else None,
                        msg.text,
                        json.dumps(
                            [r.model_dump(mode="json") for r in msg.reactions],
                            ensure_ascii=False,
                        ),
                        msg.reply_to_id,
                        msg.shared_link,
                        int(msg.is_unsent),
                        msg.call_duration,
                    ),
                )
                counts["messages"] += 1
                if msg.media:
                    counts["media"] += _insert_media_rows(
                        conn,
                        "message",
                        f"{thread.thread_id}#{seq}",
                        thread.platform,
                        msg.media,
                    )
                if msg.text or msg.sender:
                    conn.execute(
                        """
                        INSERT INTO search_fts (entity_type, entity_id, platform, author, body, timestamp_unix)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "message",
                            thread.thread_id,
                            thread.platform,
                            msg.sender,
                            msg.text or "",
                            msg.timestamp_unix or 0,
                        ),
                    )

        # Ensure zero -wal/-shm sidecar files so :ro TrueNAS mounts open cleanly
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("VACUUM;")
        conn.commit()
        conn.close()
        conn = None

        db_long = to_long_path(paths.db_path)
        os.replace(tmp_path, db_long)
        tmp_path = None

        for side in ("-wal", "-shm", "-journal"):
            side_path = to_long_path(Path(f"{paths.db_path}{side}"))
            if os.path.exists(side_path):
                try:
                    os.remove(side_path)
                except OSError:
                    pass

        return IndexBuildResult(db_path=paths.db_path, manifest_path=None, counts=counts)
    finally:
        if conn is not None:
            conn.close()
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """Open archive.sqlite strictly read-only with immutable=1 (safe on :ro mounts)."""
    resolved_posix = Path(db_path).resolve().as_posix()
    quoted = urllib.parse.quote(resolved_posix, safe="/:")
    uri = f"file:{quoted}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def search_archive(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Run hybrid Korean search (FTS5 trigram for >=3 chars, INSTR fallback for 1-2 chars)."""
    q = query.strip()
    if not q:
        return []

    if len(q) >= 3:
        fts_expr = '"' + q.replace('"', '""') + '"'
        rows = conn.execute(
            """
            SELECT entity_type, entity_id, platform, author, body, timestamp_unix
            FROM search_fts
            WHERE search_fts MATCH ?
            ORDER BY bm25(search_fts), timestamp_unix DESC
            LIMIT ?
            """,
            (fts_expr, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT entity_type, entity_id, platform, author, body, timestamp_unix
            FROM search_fts
            WHERE instr(lower(body), lower(?)) > 0
               OR instr(lower(author), lower(?)) > 0
            ORDER BY timestamp_unix DESC
            LIMIT ?
            """,
            (q, q, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def write_manifest_sha256(archive_root: Path) -> tuple[Path, int]:
    """Generate Linux `sha256sum`-compatible `manifest.sha256` for all files in archive_root."""
    paths = ArchivePaths(archive_root)
    rel_files: list[tuple[str, Path]] = []

    for dirpath, _dirnames, filenames in os.walk(to_long_path(paths.root)):
        dp = Path(dirpath)
        for fn in filenames:
            full = dp / fn
            rel = full.relative_to(Path(to_long_path(paths.root))).as_posix()
            if rel == "manifest.sha256" or rel.endswith(".tmp"):
                continue
            rel_norm = normalize_rel_path(rel)
            rel_files.append((rel_norm, full))

    rel_files.sort(key=lambda item: item[0])
    lines = [f"{sha256_file(full)}  {rel_norm}\n" for rel_norm, full in rel_files]
    atomic_write_bytes(paths.manifest_path, "".join(lines).encode("utf-8"))
    return paths.manifest_path, len(lines)


def verify_manifest_sha256(archive_root: Path) -> tuple[int, list[str]]:
    """Verify every file listed in `manifest.sha256` exists and matches its SHA-256 digest."""
    paths = ArchivePaths(archive_root)
    manifest_long = to_long_path(paths.manifest_path)
    if not os.path.exists(manifest_long):
        return 0, ["Missing manifest.sha256"]

    with open(manifest_long, "rb") as handle:
        raw_lines = handle.read().decode("utf-8").splitlines()

    checked = 0
    failures: list[str] = []
    for line in raw_lines:
        if not line.strip():
            continue
        if "  " not in line:
            failures.append(f"Malformed manifest line: {line!r}")
            continue
        expected_sha, raw_rel = line.split("  ", 1)
        try:
            rel_norm = normalize_rel_path(raw_rel)
        except ValueError as exc:
            failures.append(f"Unsafe path in manifest ({raw_rel!r}): {exc}")
            continue

        checked += 1
        target = paths.root.joinpath(*rel_norm.split("/"))
        if not os.path.exists(to_long_path(target)):
            failures.append(f"Missing file: {rel_norm}")
            continue
        actual_sha = sha256_file(target)
        if actual_sha.lower() != expected_sha.lower():
            failures.append(f"SHA-256 mismatch for {rel_norm}")

    return checked, failures
