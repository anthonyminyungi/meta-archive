"""Read-only SQLite query layer for archive.sqlite (:ro mount safe, FTS5 + short Korean fallback)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = "1"

_YEAR_EXPR = (
    "CASE WHEN created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*' "
    "THEN CAST(substr(created_at, 1, 4) AS INTEGER) "
    "ELSE CAST(strftime('%Y', timestamp_unix, 'unixepoch', '+9 hours') AS INTEGER) END"
)
_MONTH_EXPR = (
    "CASE WHEN created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]*' "
    "THEN CAST(substr(created_at, 6, 2) AS INTEGER) "
    "ELSE CAST(strftime('%m', timestamp_unix, 'unixepoch', '+9 hours') AS INTEGER) END"
)


class ArchiveNotReadyError(FileNotFoundError):
    """Raised when archive.sqlite does not exist yet in ARCHIVE_DIR."""


class SchemaVersionError(RuntimeError):
    """Raised when archive.sqlite schema_version is missing or unsupported."""


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _build_comment_tree(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    last_at_depth: dict[int, dict[str, Any]] = {}

    for row in rows:
        node: dict[str, Any] = {
            "id": row["id"],
            "comment_id": row["comment_id"],
            "post_id": row["post_id"],
            "parent_comment_id": row["parent_comment_id"],
            "platform": row["platform"],
            "depth": row["depth"],
            "author_username": row["author_username"],
            "author_name": row["author_name"],
            "text": row["text"] or "",
            "created_at": row["created_at"],
            "timestamp_unix": row["timestamp_unix"],
            "like_count": row["like_count"] or 0,
            "media": _loads_json(row["media_json"], []),
            "stickers": _loads_json(row["stickers_json"], []),
            "reactions": _loads_json(row["reactions_json"], None),
            "replies": [],
        }

        parent_id = row["parent_comment_id"]
        depth = int(row["depth"] or 0)
        attached = False
        if parent_id and parent_id != row["comment_id"] and parent_id in by_id:
            by_id[parent_id]["replies"].append(node)
            attached = True
        elif depth > 0 and (depth - 1) in last_at_depth:
            last_at_depth[depth - 1]["replies"].append(node)
            attached = True

        if not attached:
            roots.append(node)

        if row["comment_id"]:
            by_id[row["comment_id"]] = node
        last_at_depth[depth] = node

    return roots


class ArchiveReader:
    """Thread-safe read-only reader for archive.sqlite."""

    def __init__(self, archive_dir: Path) -> None:
        self.archive_dir = Path(archive_dir)
        self.db_path = self.archive_dir / "archive.sqlite"

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.is_file():
            raise ArchiveNotReadyError(f"archive.sqlite not found: {self.db_path}")

        resolved_posix = self.db_path.resolve().as_posix()
        quoted = urllib.parse.quote(resolved_posix, safe="/:")
        uri = f"file:{quoted}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _read_schema_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            rows = conn.execute("SELECT key, value FROM schema_meta").fetchall()
        except sqlite3.Error as exc:
            raise SchemaVersionError(f"Unable to read schema_meta: {exc}") from exc
        meta = {row["key"]: row["value"] for row in rows}
        version = meta.get("schema_version")
        if version != SUPPORTED_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Unsupported archive schema_version={version!r} (expected {SUPPORTED_SCHEMA_VERSION!r})"
            )
        return meta

    def check_health(self) -> dict[str, Any]:
        with self._connect() as conn:
            meta = self._read_schema_meta(conn)
            return {
                "status": "ok",
                "schema_version": meta["schema_version"],
                "built_at": meta.get("built_at"),
                "pipeline_version": meta.get("pipeline_version"),
                "archive_dir": str(self.archive_dir),
            }

    def get_overview(self) -> dict[str, Any]:
        with self._connect() as conn:
            meta = self._read_schema_meta(conn)
            tables = (
                "profiles",
                "posts",
                "media",
                "comments",
                "likes",
                "stories",
                "connections",
                "message_threads",
                "messages",
            )
            totals: dict[str, int] = {}
            by_platform: dict[str, dict[str, int]] = {"instagram": {}, "facebook": {}}
            for tbl in tables:
                totals[tbl] = int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
                for row in conn.execute(
                    f"SELECT platform, COUNT(*) AS cnt FROM {tbl} GROUP BY platform"
                ).fetchall():
                    plat = row["platform"]
                    by_platform.setdefault(plat, {})[tbl] = int(row["cnt"])

            return {
                "schema_version": meta["schema_version"],
                "built_at": meta.get("built_at"),
                "pipeline_version": meta.get("pipeline_version"),
                "totals": totals,
                "by_platform": by_platform,
            }

    def get_profiles(self, platform: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            if platform:
                rows = conn.execute(
                    "SELECT * FROM profiles WHERE platform = ? ORDER BY id ASC",
                    (platform,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM profiles ORDER BY platform ASC, id ASC").fetchall()

            return [
                {
                    "id": row["id"],
                    "platform": row["platform"],
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "full_name": row["full_name"],
                    "bio": row["bio"],
                    "avatar_local_path": row["avatar_local_path"],
                    "counts": _loads_json(row["counts_json"], {}),
                    "exported_at": row["exported_at"],
                }
                for row in rows
            ]

    def _fetch_media_map(
        self,
        conn: sqlite3.Connection,
        owner_type: str,
        owner_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not owner_ids:
            return {}
        placeholders = ",".join("?" for _ in owner_ids)
        rows = conn.execute(
            f"""
            SELECT owner_id, seq, media_id, media_type, local_path, xmp_path,
                   width, height, duration_sec, has_audio, is_audio_muted,
                   taken_at_exif, gps_lat, gps_lng, byte_size, is_carousel_cover
            FROM media
            WHERE owner_type = ? AND owner_id IN ({placeholders})
            ORDER BY seq ASC, id ASC
            """,
            [owner_type, *owner_ids],
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["owner_id"], []).append(
                {
                    "seq": row["seq"],
                    "media_id": row["media_id"],
                    "media_type": row["media_type"],
                    "local_path": row["local_path"],
                    "xmp_path": row["xmp_path"],
                    "width": row["width"],
                    "height": row["height"],
                    "duration_sec": row["duration_sec"],
                    "has_audio": bool(row["has_audio"]) if row["has_audio"] is not None else None,
                    "is_audio_muted": (
                        bool(row["is_audio_muted"]) if row["is_audio_muted"] is not None else None
                    ),
                    "taken_at_exif": row["taken_at_exif"],
                    "gps_lat": row["gps_lat"],
                    "gps_lng": row["gps_lng"],
                    "byte_size": row["byte_size"],
                    "is_carousel_cover": bool(row["is_carousel_cover"]),
                }
            )
        return grouped

    @staticmethod
    def _serialize_post_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "post_id": row["post_id"],
            "platform": row["platform"],
            "shortcode": row["shortcode"],
            "url": row["url"],
            "post_type": row["post_type"],
            "author_username": row["author_username"],
            "author_name": row["author_name"],
            "created_at": row["created_at"],
            "timestamp_unix": row["timestamp_unix"],
            "caption": row["caption"] or "",
            "hashtags": _loads_json(row["hashtags_json"], []),
            "location": (
                {
                    "name": row["location_name"],
                    "lat": row["location_lat"],
                    "lng": row["location_lng"],
                }
                if row["location_name"] or row["location_lat"] is not None
                else None
            ),
            "tagged_users": _loads_json(row["tagged_users_json"], []),
            "likes_count": row["likes_count"] or 0,
            "likes_is_truncated": bool(row["likes_is_truncated"]),
            "reactions_total": row["reactions_total"] or 0,
            "reactions": _loads_json(row["reactions_json"], {}),
            "album_name": row["album_name"],
            "shared_post": _loads_json(row["shared_post_json"], None),
            "audience": row["audience"],
            "is_archived": bool(row["is_archived"]),
            "is_collab": bool(row["is_collab"]),
            "source": row["source"],
        }

    def list_posts(
        self,
        platform: str | None = None,
        offset: int = 0,
        limit: int = 30,
        year: int | None = None,
        month: int | None = None,
        archived: bool | None = None,
        post_type: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if year is not None:
            clauses.append(f"{_YEAR_EXPR} = ?")
            params.append(year)
        if month is not None:
            clauses.append(f"{_MONTH_EXPR} = ?")
            params.append(month)
        if archived is not None:
            clauses.append("COALESCE(is_archived, 0) = ?")
            params.append(1 if archived else 0)
        if post_type:
            clauses.append("post_type = ?")
            params.append(post_type)
        if album:
            clauses.append("album_name = ?")
            params.append(album)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            self._read_schema_meta(conn)
            total = int(conn.execute(f"SELECT COUNT(*) FROM posts {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT post_id, platform, shortcode, url, post_type,
                       author_username, author_name, created_at, timestamp_unix,
                       caption, hashtags_json, location_name, location_lat, location_lng,
                       tagged_users_json, likes_count, likes_is_truncated,
                       reactions_total, reactions_json, album_name, shared_post_json,
                       audience, is_archived, is_collab, source
                FROM posts
                {where}
                ORDER BY COALESCE(timestamp_unix, 0) DESC, post_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

            post_ids = [r["post_id"] for r in rows]
            media_map = self._fetch_media_map(conn, "post", post_ids)
            comment_counts: dict[str, int] = {}
            if post_ids:
                ph = ",".join("?" for _ in post_ids)
                for crow in conn.execute(
                    f"SELECT post_id, COUNT(*) AS cnt FROM comments WHERE post_id IN ({ph}) GROUP BY post_id",
                    post_ids,
                ).fetchall():
                    comment_counts[crow["post_id"]] = int(crow["cnt"])

            items = []
            for row in rows:
                item = self._serialize_post_row(row)
                item["media"] = media_map.get(row["post_id"], [])
                item["comments_count"] = comment_counts.get(row["post_id"], 0)
                items.append(item)

            return {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": items,
            }

    def get_post_detail(self, post_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            row = conn.execute(
                """
                SELECT post_id, platform, shortcode, url, post_type,
                       author_username, author_name, created_at, timestamp_unix,
                       caption, hashtags_json, location_name, location_lat, location_lng,
                       tagged_users_json, likes_count, likes_is_truncated,
                       reactions_total, reactions_json, album_name, shared_post_json,
                       audience, is_archived, is_collab, source
                FROM posts
                WHERE post_id = ?
                """,
                (post_id,),
            ).fetchone()
            if row is None:
                return None

            media_map = self._fetch_media_map(conn, "post", [post_id])
            comment_rows = conn.execute(
                """
                SELECT id, comment_id, post_id, parent_comment_id, platform, depth,
                       author_username, author_name, text, created_at, timestamp_unix,
                       like_count, media_json, stickers_json, reactions_json
                FROM comments
                WHERE post_id = ?
                ORDER BY id ASC
                """,
                (post_id,),
            ).fetchall()
            like_rows = conn.execute(
                """
                SELECT user_id, username, full_name, profile_url
                FROM likes
                WHERE post_id = ?
                ORDER BY id ASC
                """,
                (post_id,),
            ).fetchall()

            detail = self._serialize_post_row(row)
            detail["media"] = media_map.get(post_id, [])
            detail["comments"] = _build_comment_tree(comment_rows)
            detail["comments_count"] = len(comment_rows)
            detail["likes_users"] = [
                {
                    "user_id": lr["user_id"],
                    "username": lr["username"],
                    "full_name": lr["full_name"],
                    "profile_url": lr["profile_url"],
                }
                for lr in like_rows
            ]
            return detail

    def list_highlights(self, platform: str = "instagram") -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            rows = conn.execute(
                """
                SELECT story_id, highlight_id, highlight_title, highlight_cover, timestamp_unix
                FROM stories
                WHERE platform = ? AND highlight_id IS NOT NULL AND highlight_id != ''
                ORDER BY COALESCE(timestamp_unix, 0) DESC, story_id ASC
                """,
                (platform,),
            ).fetchall()
            story_ids = [r["story_id"] for r in rows]
            media_map = self._fetch_media_map(conn, "story", story_ids)

            groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                hid = row["highlight_id"]
                media_list = media_map.get(row["story_id"], [])
                fallback_cover = media_list[0]["local_path"] if media_list else None
                ts = row["timestamp_unix"] or 0
                if hid not in groups:
                    groups[hid] = {
                        "highlight_id": hid,
                        "highlight_title": row["highlight_title"] or hid,
                        "highlight_cover": row["highlight_cover"] or fallback_cover,
                        "count": 0,
                        "latest_timestamp_unix": ts,
                    }
                grp = groups[hid]
                grp["count"] += 1
                if not grp["highlight_cover"] and (row["highlight_cover"] or fallback_cover):
                    grp["highlight_cover"] = row["highlight_cover"] or fallback_cover
                if not grp["highlight_title"] and row["highlight_title"]:
                    grp["highlight_title"] = row["highlight_title"]
                if ts > (grp["latest_timestamp_unix"] or 0):
                    grp["latest_timestamp_unix"] = ts

            return sorted(
                groups.values(),
                key=lambda item: (item["latest_timestamp_unix"] or 0, item["highlight_title"]),
                reverse=True,
            )

    def list_stories(
        self,
        platform: str = "instagram",
        highlight_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["platform = ?"]
        params: list[Any] = [platform]
        if highlight_id:
            clauses.append("highlight_id = ?")
            params.append(highlight_id)
        where = f"WHERE {' AND '.join(clauses)}"

        with self._connect() as conn:
            self._read_schema_meta(conn)
            total = int(conn.execute(f"SELECT COUNT(*) FROM stories {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT story_id, platform, created_at, timestamp_unix, source,
                       highlight_id, highlight_title, highlight_cover,
                       location_name, stickers_json, likes_count
                FROM stories
                {where}
                ORDER BY COALESCE(timestamp_unix, 0) DESC, story_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

            story_ids = [r["story_id"] for r in rows]
            media_map = self._fetch_media_map(conn, "story", story_ids)
            items = []
            for row in rows:
                media_list = media_map.get(row["story_id"], [])
                items.append(
                    {
                        "story_id": row["story_id"],
                        "platform": row["platform"],
                        "created_at": row["created_at"],
                        "timestamp_unix": row["timestamp_unix"],
                        "source": row["source"],
                        "highlight_id": row["highlight_id"],
                        "highlight_title": row["highlight_title"],
                        "highlight_cover": row["highlight_cover"],
                        "location_name": row["location_name"],
                        "stickers": _loads_json(row["stickers_json"], []),
                        "likes_count": row["likes_count"] or 0,
                        "media": media_list[0] if media_list else None,
                    }
                )
            return {"total": total, "offset": offset, "limit": limit, "items": items}

    def list_connections(
        self,
        platform: str | None = None,
        relation: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if relation:
            clauses.append("relation = ?")
            params.append(relation)
        if q and q.strip():
            needle = q.strip()
            clauses.append(
                "(instr(lower(COALESCE(username, '')), lower(?)) > 0 "
                "OR instr(lower(COALESCE(full_name, '')), lower(?)) > 0)"
            )
            params.extend([needle, needle])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            self._read_schema_meta(conn)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM connections {where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT id, platform, relation, user_id, username, full_name,
                       profile_url, avatar_local_path, connected_at, timestamp_unix,
                       match_confidence
                FROM connections
                {where}
                ORDER BY COALESCE(timestamp_unix, 0) DESC,
                         COALESCE(username, full_name, '') COLLATE NOCASE ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [dict(row) for row in rows]
            return {"total": total, "offset": offset, "limit": limit, "items": items}

    def list_albums(self, platform: str = "facebook") -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            rows = conn.execute(
                """
                SELECT album_name,
                       COUNT(*) AS count,
                       MAX(timestamp_unix) AS latest_timestamp_unix,
                       (
                           SELECT m.local_path
                           FROM posts p2
                           JOIN media m ON m.owner_type = 'post' AND m.owner_id = p2.post_id
                           WHERE p2.platform = posts.platform AND p2.album_name = posts.album_name
                           ORDER BY COALESCE(p2.timestamp_unix, 0) DESC, m.seq ASC
                           LIMIT 1
                       ) AS cover_local_path
                FROM posts
                WHERE platform = ? AND album_name IS NOT NULL AND album_name != ''
                GROUP BY album_name
                ORDER BY COALESCE(latest_timestamp_unix, 0) DESC, album_name ASC
                """,
                (platform,),
            ).fetchall()
            return [
                {
                    "album_name": row["album_name"],
                    "count": int(row["count"]),
                    "latest_timestamp_unix": row["latest_timestamp_unix"],
                    "cover_local_path": row["cover_local_path"],
                }
                for row in rows
            ]

    def list_timeline_months(self, platform: str = "facebook") -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            rows = conn.execute(
                f"""
                SELECT {_YEAR_EXPR} AS year, {_MONTH_EXPR} AS month, COUNT(*) AS count
                FROM posts
                WHERE platform = ? AND (created_at IS NOT NULL OR timestamp_unix IS NOT NULL)
                GROUP BY year, month
                HAVING year IS NOT NULL AND month IS NOT NULL
                ORDER BY year DESC, month DESC
                """,
                (platform,),
            ).fetchall()
            return [
                {"year": int(row["year"]), "month": int(row["month"]), "count": int(row["count"])}
                for row in rows
            ]

    def list_threads(
        self,
        platform: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if q and q.strip():
            needle = q.strip()
            clauses.append(
                "(instr(lower(COALESCE(title, '')), lower(?)) > 0 "
                "OR instr(lower(COALESCE(participants_json, '')), lower(?)) > 0)"
            )
            params.extend([needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            self._read_schema_meta(conn)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM message_threads {where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT thread_id, platform, title, participants_json,
                       is_e2ee, is_pending, message_count, last_timestamp_unix
                FROM message_threads
                {where}
                ORDER BY COALESCE(last_timestamp_unix, 0) DESC, thread_id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            items = [
                {
                    "thread_id": row["thread_id"],
                    "platform": row["platform"],
                    "title": row["title"] or row["thread_id"],
                    "participants": _loads_json(row["participants_json"], []),
                    "is_e2ee": bool(row["is_e2ee"]),
                    "is_pending": bool(row["is_pending"]),
                    "message_count": row["message_count"] or 0,
                    "last_timestamp_unix": row["last_timestamp_unix"],
                }
                for row in rows
            ]
            return {"total": total, "offset": offset, "limit": limit, "items": items}

    def get_thread_messages(
        self,
        thread_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            trow = conn.execute(
                """
                SELECT thread_id, platform, title, participants_json,
                       is_e2ee, is_pending, message_count, last_timestamp_unix
                FROM message_threads
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if trow is None:
                return None

            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT id, thread_id, message_id, platform, seq, sender,
                       timestamp_unix, timestamp_ms, created_at, text,
                       reactions_json, reply_to_id, shared_link, is_unsent, call_duration
                FROM messages
                WHERE thread_id = ?
                ORDER BY COALESCE(timestamp_ms, timestamp_unix * 1000, 0) ASC, seq ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (thread_id, limit, offset),
            ).fetchall()

            owner_ids = [f"{thread_id}#{row['seq']}" for row in rows]
            media_map = self._fetch_media_map(conn, "message", owner_ids)

            items = []
            for row in rows:
                owner_key = f"{thread_id}#{row['seq']}"
                items.append(
                    {
                        "id": row["id"],
                        "thread_id": row["thread_id"],
                        "message_id": row["message_id"],
                        "platform": row["platform"],
                        "seq": row["seq"],
                        "sender": row["sender"],
                        "timestamp_unix": row["timestamp_unix"],
                        "timestamp_ms": row["timestamp_ms"],
                        "created_at": row["created_at"],
                        "text": row["text"] or "",
                        "reactions": _loads_json(row["reactions_json"], []),
                        "reply_to_id": row["reply_to_id"],
                        "shared_link": row["shared_link"],
                        "is_unsent": bool(row["is_unsent"]),
                        "call_duration": row["call_duration"],
                        "media": media_map.get(owner_key, []),
                    }
                )

            return {
                "thread": {
                    "thread_id": trow["thread_id"],
                    "platform": trow["platform"],
                    "title": trow["title"] or trow["thread_id"],
                    "participants": _loads_json(trow["participants_json"], []),
                    "is_e2ee": bool(trow["is_e2ee"]),
                    "is_pending": bool(trow["is_pending"]),
                    "message_count": trow["message_count"] or total,
                    "last_timestamp_unix": trow["last_timestamp_unix"],
                },
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": items,
            }

    def search(
        self,
        query: str,
        platform: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        q = query.strip()
        if not q:
            return {"query": "", "mode": "empty", "total": 0, "items": []}

        extra_clauses: list[str] = []
        extra_params: list[Any] = []
        if platform:
            extra_clauses.append("platform = ?")
            extra_params.append(platform)
        if entity_type:
            extra_clauses.append("entity_type = ?")
            extra_params.append(entity_type)
        extra_sql = f" AND {' AND '.join(extra_clauses)}" if extra_clauses else ""

        with self._connect() as conn:
            self._read_schema_meta(conn)
            if len(q) >= 3:
                mode = "fts5_trigram"
                fts_expr = '"' + q.replace('"', '""') + '"'
                rows = conn.execute(
                    f"""
                    SELECT entity_type, entity_id, platform, author, body, timestamp_unix
                    FROM search_fts
                    WHERE search_fts MATCH ?{extra_sql}
                    ORDER BY bm25(search_fts), timestamp_unix DESC
                    LIMIT ?
                    """,
                    [fts_expr, *extra_params, limit],
                ).fetchall()
            else:
                mode = "instr_short"
                rows = conn.execute(
                    f"""
                    SELECT entity_type, entity_id, platform, author, body, timestamp_unix
                    FROM search_fts
                    WHERE (instr(lower(body), lower(?)) > 0 OR instr(lower(author), lower(?)) > 0)
                    {extra_sql}
                    ORDER BY timestamp_unix DESC
                    LIMIT ?
                    """,
                    [q, q, *extra_params, limit],
                ).fetchall()

            items = [dict(row) for row in rows]
            return {
                "query": q,
                "mode": mode,
                "total": len(items),
                "items": items,
            }

    def get_person_activity(self, identifier: str, limit: int = 50) -> dict[str, Any]:
        ident = identifier.strip()
        if not ident:
            return {
                "identifier": identifier,
                "connections": [],
                "comments": [],
                "liked_posts": [],
                "messages": [],
            }

        with self._connect() as conn:
            self._read_schema_meta(conn)
            conn_rows = conn.execute(
                """
                SELECT id, platform, relation, user_id, username, full_name,
                       profile_url, avatar_local_path, connected_at, timestamp_unix,
                       match_confidence
                FROM connections
                WHERE lower(COALESCE(username, '')) = lower(?)
                   OR lower(COALESCE(full_name, '')) = lower(?)
                ORDER BY platform ASC, relation ASC
                """,
                (ident, ident),
            ).fetchall()

            aliases = {ident.lower()}
            for crow in conn_rows:
                for col in ("username", "full_name"):
                    val = (crow[col] or "").strip().lower()
                    if val:
                        aliases.add(val)

            alias_list = sorted(aliases)
            ph = ",".join("?" for _ in alias_list)

            comment_rows = conn.execute(
                f"""
                SELECT c.post_id, c.platform, c.author_username, c.author_name,
                       c.text, c.created_at, c.timestamp_unix, c.like_count,
                       p.caption AS post_caption
                FROM comments c
                LEFT JOIN posts p ON p.post_id = c.post_id
                WHERE lower(COALESCE(c.author_username, '')) IN ({ph})
                   OR lower(COALESCE(c.author_name, '')) IN ({ph})
                ORDER BY COALESCE(c.timestamp_unix, 0) DESC, c.id DESC
                LIMIT ?
                """,
                [*alias_list, *alias_list, limit],
            ).fetchall()

            liked_rows = conn.execute(
                f"""
                SELECT l.post_id, l.platform, l.username, l.full_name,
                       p.caption, p.created_at, p.timestamp_unix, p.post_type
                FROM likes l
                LEFT JOIN posts p ON p.post_id = l.post_id
                WHERE lower(COALESCE(l.username, '')) IN ({ph})
                   OR lower(COALESCE(l.full_name, '')) IN ({ph})
                ORDER BY COALESCE(p.timestamp_unix, 0) DESC, l.id DESC
                LIMIT ?
                """,
                [*alias_list, *alias_list, limit],
            ).fetchall()

            msg_rows = conn.execute(
                f"""
                SELECT m.thread_id, m.platform, m.sender, m.text, m.created_at,
                       m.timestamp_unix, mt.title AS thread_title
                FROM messages m
                LEFT JOIN message_threads mt ON mt.thread_id = m.thread_id
                WHERE lower(COALESCE(m.sender, '')) IN ({ph})
                ORDER BY COALESCE(m.timestamp_unix, 0) DESC, m.id DESC
                LIMIT ?
                """,
                [*alias_list, limit],
            ).fetchall()

            return {
                "identifier": ident,
                "connections": [dict(r) for r in conn_rows],
                "comments": [dict(r) for r in comment_rows],
                "liked_posts": [dict(r) for r in liked_rows],
                "messages": [dict(r) for r in msg_rows],
            }
