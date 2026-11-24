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
        self._col_cache: dict[str, set[str]] = {}
        self._table_cache: set[str] | None = None
        self._avatar_cache: dict[str, Any] | None = None

    def _get_avatar_index(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if self._avatar_cache is not None:
            return self._avatar_cache

        by_plat_user: dict[tuple[str, str], str] = {}
        by_plat_name: dict[tuple[str, str], str] = {}
        name_candidates: dict[str, set[str]] = {}
        owner_names: dict[str, set[str]] = {}
        owner_avatars: dict[str, str] = {}

        if self._has_table(conn, "profiles"):
            for r in conn.execute(
                "SELECT platform, username, full_name, avatar_local_path FROM profiles"
            ).fetchall():
                plat = (r["platform"] or "").lower()
                u = (r["username"] or "").strip().lower()
                n = (r["full_name"] or "").strip().lower()
                av = (r["avatar_local_path"] or "").strip()
                if u:
                    owner_names.setdefault(plat, set()).add(u)
                if n:
                    owner_names.setdefault(plat, set()).add(n)
                if av:
                    owner_avatars[plat] = av
                    if u:
                        by_plat_user[(plat, u)] = av
                    if n:
                        by_plat_name[(plat, n)] = av
                        name_candidates.setdefault(n, set()).add(av)

        if self._has_table(conn, "connections"):
            for r in conn.execute(
                "SELECT platform, username, full_name, avatar_local_path FROM connections "
                "WHERE avatar_local_path IS NOT NULL AND avatar_local_path != ''"
            ).fetchall():
                plat = (r["platform"] or "").lower()
                u = (r["username"] or "").strip().lower()
                n = (r["full_name"] or "").strip().lower()
                av = (r["avatar_local_path"] or "").strip()
                if not av:
                    continue
                if u:
                    by_plat_user.setdefault((plat, u), av)
                if n:
                    by_plat_name.setdefault((plat, n), av)
                    name_candidates.setdefault(n, set()).add(av)

        by_name_any = {
            name: next(iter(avs)) for name, avs in name_candidates.items() if len(avs) == 1
        }
        self._avatar_cache = {
            "by_plat_user": by_plat_user,
            "by_plat_name": by_plat_name,
            "by_name_any": by_name_any,
            "owner_names": owner_names,
            "owner_avatars": owner_avatars,
        }
        return self._avatar_cache

    def _resolve_avatar(
        self,
        conn: sqlite3.Connection,
        platform: str | None,
        *,
        username: str | None = None,
        full_name: str | None = None,
    ) -> str | None:
        idx = self._get_avatar_index(conn)
        plat = (platform or "").lower()
        u = (username or "").strip().lower()
        n = (full_name or "").strip().lower()
        n_clean = n[:-1].strip() if n.endswith("님") and len(n) > 1 else n

        if u and plat and (plat, u) in idx["by_plat_user"]:
            return idx["by_plat_user"][(plat, u)]
        if n and plat and (plat, n) in idx["by_plat_name"]:
            return idx["by_plat_name"][(plat, n)]
        if n_clean and plat and (plat, n_clean) in idx["by_plat_name"]:
            return idx["by_plat_name"][(plat, n_clean)]
        if u:
            for (p, ku), av in idx["by_plat_user"].items():
                if ku == u:
                    return av
        if n and n in idx["by_name_any"]:
            return idx["by_name_any"][n]
        if n_clean and n_clean in idx["by_name_any"]:
            return idx["by_name_any"][n_clean]
        return None

    def _resolve_thread_avatar(
        self,
        conn: sqlite3.Connection,
        platform: str,
        existing_avatar: str | None,
        title: str | None,
        participants: list[dict[str, Any]],
    ) -> str | None:
        if existing_avatar:
            return existing_avatar
        idx = self._get_avatar_index(conn)
        plat = (platform or "").lower()
        owners = idx["owner_names"].get(plat, set())
        non_owners: list[dict[str, Any]] = []
        for p in participants:
            if not isinstance(p, dict):
                continue
            u = (p.get("username") or "").strip().lower()
            n = (p.get("full_name") or "").strip().lower()
            if (u and u in owners) or (n and n in owners):
                continue
            non_owners.append(p)
        if len(non_owners) == 1:
            p = non_owners[0]
            av = p.get("avatar_local_path") or self._resolve_avatar(
                conn, plat, username=p.get("username"), full_name=p.get("full_name")
            )
            if av:
                return av
        if title and len(non_owners) <= 1:
            return self._resolve_avatar(conn, plat, full_name=title)
        return None

    def _has_table(self, conn: sqlite3.Connection, table: str) -> bool:
        if self._table_cache is None:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self._table_cache = {r["name"] for r in rows}
        return table in self._table_cache

    def _table_cols(self, conn: sqlite3.Connection, table: str) -> set[str]:
        if table not in self._col_cache:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            self._col_cache[table] = {r["name"] for r in rows}
        return self._col_cache[table]

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
            for tbl in (*tables, "activities"):
                if not self._has_table(conn, tbl):
                    totals[tbl] = 0
                    continue
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
        keys = row.keys()
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
            "activity_title": row["activity_title"] if "activity_title" in keys else None,
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
            "group_name": row["group_name"] if "group_name" in keys else None,
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
        if post_type == "original":
            clauses.append("COALESCE(post_type, 'post') NOT IN ('shared', 'group_post')")
        elif post_type:
            clauses.append("post_type = ?")
            params.append(post_type)
        if album:
            clauses.append("album_name = ?")
            params.append(album)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            self._read_schema_meta(conn)
            pcols = self._table_cols(conn, "posts")
            extra_pcols = "".join(
                f", {c}" for c in ("activity_title", "group_name") if c in pcols
            )
            total = int(conn.execute(f"SELECT COUNT(*) FROM posts {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT post_id, platform, shortcode, url, post_type,
                       author_username, author_name, created_at, timestamp_unix,
                       caption, hashtags_json, location_name, location_lat, location_lng,
                       tagged_users_json, likes_count, likes_is_truncated,
                       reactions_total, reactions_json, album_name, shared_post_json,
                       audience, is_archived, is_collab, source{extra_pcols}
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
                item["author_avatar_local_path"] = self._resolve_avatar(
                    conn,
                    item["platform"],
                    username=item["author_username"],
                    full_name=item["author_name"],
                )
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
            pcols = self._table_cols(conn, "posts")
            extra_pcols = "".join(
                f", {c}" for c in ("activity_title", "group_name") if c in pcols
            )
            row = conn.execute(
                f"""
                SELECT post_id, platform, shortcode, url, post_type,
                       author_username, author_name, created_at, timestamp_unix,
                       caption, hashtags_json, location_name, location_lat, location_lng,
                       tagged_users_json, likes_count, likes_is_truncated,
                       reactions_total, reactions_json, album_name, shared_post_json,
                       audience, is_archived, is_collab, source{extra_pcols}
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
            detail["author_avatar_local_path"] = self._resolve_avatar(
                conn,
                detail["platform"],
                username=detail["author_username"],
                full_name=detail["author_name"],
            )
            detail["media"] = media_map.get(post_id, [])

            def _attach_comment_avatars(nodes: list[dict[str, Any]]) -> None:
                for node in nodes:
                    node["author_avatar_local_path"] = self._resolve_avatar(
                        conn,
                        node.get("platform") or detail["platform"],
                        username=node.get("author_username"),
                        full_name=node.get("author_name"),
                    )
                    if node.get("replies"):
                        _attach_comment_avatars(node["replies"])

            comment_tree = _build_comment_tree(comment_rows)
            _attach_comment_avatars(comment_tree)
            detail["comments"] = comment_tree
            detail["comments_count"] = len(comment_rows)
            detail["likes_users"] = [
                {
                    "user_id": lr["user_id"],
                    "username": lr["username"],
                    "full_name": lr["full_name"],
                    "profile_url": lr["profile_url"],
                    "avatar_local_path": self._resolve_avatar(
                        conn,
                        detail["platform"],
                        username=lr["username"],
                        full_name=lr["full_name"],
                    ),
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
        story_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = ["platform = ?"]
        params: list[Any] = [platform]
        if highlight_id:
            clauses.append("highlight_id = ?")
            params.append(highlight_id)
        if story_id:
            clauses.append("story_id = ?")
            params.append(story_id)
        where = f"WHERE {' AND '.join(clauses)}"

        with self._connect() as conn:
            self._read_schema_meta(conn)
            scols = self._table_cols(conn, "stories")
            cap_col = ", caption" if "caption" in scols else ""
            total = int(conn.execute(f"SELECT COUNT(*) FROM stories {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT story_id, platform, created_at, timestamp_unix, source,
                       highlight_id, highlight_title, highlight_cover,
                       location_name, stickers_json, likes_count{cap_col}
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
                rkeys = row.keys()
                media_list = media_map.get(row["story_id"], [])
                items.append(
                    {
                        "story_id": row["story_id"],
                        "platform": row["platform"],
                        "created_at": row["created_at"],
                        "timestamp_unix": row["timestamp_unix"],
                        "caption": (row["caption"] or "") if "caption" in rkeys else "",
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
        sort: str = "recent",
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
        if sort == "messages":
            order_by = "COALESCE(message_count, 0) DESC, COALESCE(last_timestamp_unix, 0) DESC, thread_id ASC"
        elif sort == "oldest":
            order_by = "COALESCE(last_timestamp_unix, 0) ASC, thread_id ASC"
        else:
            order_by = "COALESCE(last_timestamp_unix, 0) DESC, thread_id ASC"

        with self._connect() as conn:
            self._read_schema_meta(conn)
            tcols = self._table_cols(conn, "message_threads")
            av_col = ", avatar_local_path" if "avatar_local_path" in tcols else ""
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM message_threads {where}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT thread_id, platform, title, participants_json,
                       is_e2ee, is_pending, message_count, last_timestamp_unix{av_col}
                FROM message_threads
                {where}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

            thread_ids = [r["thread_id"] for r in rows]
            preview_map: dict[str, str] = {}
            if thread_ids:
                ph = ",".join("?" for _ in thread_ids)
                mcols = self._table_cols(conn, "messages")
                missed_col = ", m.is_missed_call" if "is_missed_call" in mcols else ""
                prev_rows = conn.execute(
                    f"""
                    SELECT m.thread_id, m.sender, m.text, m.shared_link, m.is_unsent, m.call_duration{missed_col}
                    FROM messages m
                    JOIN (
                        SELECT thread_id, MAX(id) AS max_id
                        FROM messages
                        WHERE thread_id IN ({ph})
                        GROUP BY thread_id
                    ) latest ON m.id = latest.max_id
                    """,
                    thread_ids,
                ).fetchall()
                for pr in prev_rows:
                    pkeys = pr.keys()
                    txt = (pr["text"] or "").strip()
                    if pr["is_unsent"]:
                        snippet = "(전송 취소된 메시지)"
                    elif txt:
                        snippet = txt.replace("\n", " ")[:64]
                    elif "is_missed_call" in pkeys and pr["is_missed_call"]:
                        snippet = "📵 부재중 통화"
                    elif pr["call_duration"]:
                        snippet = "📞 통화"
                    elif pr["shared_link"]:
                        snippet = "🔗 링크 공유"
                    else:
                        snippet = "📎 미디어 첨부"
                    sender_prefix = f"{pr['sender']}: " if pr["sender"] else ""
                    preview_map[pr["thread_id"]] = f"{sender_prefix}{snippet}"

            items = []
            for row in rows:
                rkeys = row.keys()
                participants = _loads_json(row["participants_json"], [])
                raw_av = row["avatar_local_path"] if "avatar_local_path" in rkeys else None
                resolved_av = self._resolve_thread_avatar(
                    conn,
                    row["platform"],
                    raw_av,
                    row["title"],
                    participants,
                )
                items.append(
                    {
                        "thread_id": row["thread_id"],
                        "platform": row["platform"],
                        "title": row["title"] or row["thread_id"],
                        "avatar_local_path": resolved_av,
                        "participants": participants,
                        "is_e2ee": bool(row["is_e2ee"]),
                        "is_pending": bool(row["is_pending"]),
                        "message_count": row["message_count"] or 0,
                        "last_timestamp_unix": row["last_timestamp_unix"],
                        "last_message_preview": preview_map.get(row["thread_id"], ""),
                    }
                )
            return {"total": total, "offset": offset, "limit": limit, "items": items}

    def get_thread_messages(
        self,
        thread_id: str,
        offset: int = 0,
        limit: int = 100,
        tail: bool = False,
        before_seq: int | None = None,
        around_ts: int | None = None,
        q: str | None = None,
        media_only: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            tcols = self._table_cols(conn, "message_threads")
            av_col = ", avatar_local_path" if "avatar_local_path" in tcols else ""
            trow = conn.execute(
                f"""
                SELECT thread_id, platform, title, participants_json,
                       is_e2ee, is_pending, message_count, last_timestamp_unix{av_col}
                FROM message_threads
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if trow is None:
                return None

            mcols = self._table_cols(conn, "messages")
            extra_mcols = "".join(
                f", {c}" for c in ("share_text", "share_owner", "is_missed_call") if c in mcols
            )
            filter_q = q.strip() if q and q.strip() else None

            if filter_q or media_only:
                fclauses = ["thread_id = ?"]
                fparams: list[Any] = [thread_id]
                if filter_q:
                    fclauses.append(
                        "(instr(lower(COALESCE(text, '')), lower(?)) > 0 "
                        "OR instr(lower(COALESCE(sender, '')), lower(?)) > 0)"
                    )
                    fparams.extend([filter_q, filter_q])
                if media_only:
                    fclauses.append(
                        "EXISTS (SELECT 1 FROM media md WHERE md.owner_type = 'message' "
                        "AND md.owner_id = messages.thread_id || '#' || messages.seq)"
                    )
                fwhere = " AND ".join(fclauses)
                total = int(
                    conn.execute(f"SELECT COUNT(*) FROM messages WHERE {fwhere}", fparams).fetchone()[0]
                )
                rows = conn.execute(
                    f"""
                    SELECT id, thread_id, message_id, platform, seq, sender,
                           timestamp_unix, timestamp_ms, created_at, text,
                           reactions_json, reply_to_id, shared_link, is_unsent, call_duration{extra_mcols}
                    FROM messages
                    WHERE {fwhere}
                    ORDER BY COALESCE(timestamp_ms, timestamp_unix * 1000, 0) DESC, seq DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*fparams, limit, offset],
                ).fetchall()
                eff_offset = offset
                has_older = (offset + len(rows)) < total
                has_newer = offset > 0
            else:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE thread_id = ?",
                        (thread_id,),
                    ).fetchone()[0]
                )
                eff_offset = offset
                if before_seq is not None:
                    desc_rows = conn.execute(
                        f"""
                        SELECT id, thread_id, message_id, platform, seq, sender,
                               timestamp_unix, timestamp_ms, created_at, text,
                               reactions_json, reply_to_id, shared_link, is_unsent, call_duration{extra_mcols}
                        FROM messages
                        WHERE thread_id = ? AND seq < ?
                        ORDER BY seq DESC, id DESC
                        LIMIT ?
                        """,
                        (thread_id, before_seq, limit),
                    ).fetchall()
                    rows = list(reversed(desc_rows))
                    eff_offset = int(rows[0]["seq"]) if rows else 0
                else:
                    if around_ts is not None and total > 0:
                        near_row = conn.execute(
                            """
                            SELECT seq FROM messages
                            WHERE thread_id = ?
                            ORDER BY ABS(COALESCE(timestamp_unix, 0) - ?) ASC, seq ASC
                            LIMIT 1
                            """,
                            (thread_id, around_ts),
                        ).fetchone()
                        if near_row is not None:
                            eff_offset = max(0, int(near_row["seq"]) - limit // 2)
                    elif tail:
                        eff_offset = max(0, total - limit)

                    rows = conn.execute(
                        f"""
                        SELECT id, thread_id, message_id, platform, seq, sender,
                               timestamp_unix, timestamp_ms, created_at, text,
                               reactions_json, reply_to_id, shared_link, is_unsent, call_duration{extra_mcols}
                        FROM messages
                        WHERE thread_id = ?
                        ORDER BY COALESCE(timestamp_ms, timestamp_unix * 1000, 0) ASC, seq ASC, id ASC
                        LIMIT ? OFFSET ?
                        """,
                        (thread_id, limit, eff_offset),
                    ).fetchall()
                has_older = bool(rows and int(rows[0]["seq"]) > 0)
                has_newer = bool(rows and (int(rows[-1]["seq"]) + 1) < total)

            owner_ids = [f"{thread_id}#{row['seq']}" for row in rows]
            media_map = self._fetch_media_map(conn, "message", owner_ids)

            plat = trow["platform"]
            av_idx = self._get_avatar_index(conn)
            owner_names = sorted(av_idx["owner_names"].get((plat or "").lower(), set()))
            owner_avatar = av_idx["owner_avatars"].get((plat or "").lower())

            items = []
            for row in rows:
                rkeys = row.keys()
                owner_key = f"{thread_id}#{row['seq']}"
                sender = row["sender"]
                items.append(
                    {
                        "id": row["id"],
                        "thread_id": row["thread_id"],
                        "message_id": row["message_id"],
                        "platform": row["platform"],
                        "seq": row["seq"],
                        "sender": sender,
                        "sender_avatar_local_path": self._resolve_avatar(
                            conn, plat, username=sender, full_name=sender
                        )
                        if sender
                        else None,
                        "timestamp_unix": row["timestamp_unix"],
                        "timestamp_ms": row["timestamp_ms"],
                        "created_at": row["created_at"],
                        "text": row["text"] or "",
                        "reactions": _loads_json(row["reactions_json"], []),
                        "reply_to_id": row["reply_to_id"],
                        "shared_link": row["shared_link"],
                        "share_text": row["share_text"] if "share_text" in rkeys else None,
                        "share_owner": row["share_owner"] if "share_owner" in rkeys else None,
                        "is_unsent": bool(row["is_unsent"]),
                        "is_missed_call": bool(row["is_missed_call"]) if "is_missed_call" in rkeys else False,
                        "call_duration": row["call_duration"],
                        "media": media_map.get(owner_key, []),
                    }
                )

            tkeys = trow.keys()
            participants = _loads_json(trow["participants_json"], [])
            for part in participants:
                if isinstance(part, dict) and not part.get("avatar_local_path"):
                    part["avatar_local_path"] = self._resolve_avatar(
                        conn, plat, username=part.get("username"), full_name=part.get("full_name")
                    )
            raw_av = trow["avatar_local_path"] if "avatar_local_path" in tkeys else None
            resolved_av = self._resolve_thread_avatar(conn, plat, raw_av, trow["title"], participants)
            return {
                "thread": {
                    "thread_id": trow["thread_id"],
                    "platform": plat,
                    "title": trow["title"] or trow["thread_id"],
                    "avatar_local_path": resolved_av,
                    "participants": participants,
                    "owner_names": owner_names,
                    "owner_avatar_local_path": owner_avatar,
                    "is_e2ee": bool(trow["is_e2ee"]),
                    "is_pending": bool(trow["is_pending"]),
                    "message_count": trow["message_count"] or total,
                    "last_timestamp_unix": trow["last_timestamp_unix"],
                },
                "total": total,
                "offset": eff_offset,
                "limit": limit,
                "has_older": has_older,
                "has_newer": has_newer,
                "items": items,
            }

    def list_activities(
        self,
        platform: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        activity_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._read_schema_meta(conn)
            if not self._has_table(conn, "activities"):
                return {"total": 0, "offset": offset, "limit": limit, "items": []}

            clauses: list[str] = []
            params: list[Any] = []
            if platform:
                clauses.append("platform = ?")
                params.append(platform)
            if kind:
                clauses.append("kind = ?")
                params.append(kind)
            if activity_id:
                clauses.append("activity_id = ?")
                params.append(activity_id)
            if q and q.strip():
                needle = q.strip()
                clauses.append(
                    "(instr(lower(COALESCE(text, '')), lower(?)) > 0 "
                    "OR instr(lower(COALESCE(title, '')), lower(?)) > 0 "
                    "OR instr(lower(COALESCE(target_author, '')), lower(?)) > 0 "
                    "OR instr(lower(COALESCE(group_name, '')), lower(?)) > 0)"
                )
                params.extend([needle, needle, needle, needle])
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

            total = int(conn.execute(f"SELECT COUNT(*) FROM activities {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT activity_id, platform, kind, created_at, timestamp_unix,
                       text, title, target_author, group_name, url, source
                FROM activities
                {where}
                ORDER BY COALESCE(timestamp_unix, 0) DESC, activity_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

            act_ids = [r["activity_id"] for r in rows]
            media_map = self._fetch_media_map(conn, "activity", act_ids)
            items = [
                {
                    "activity_id": r["activity_id"],
                    "platform": r["platform"],
                    "kind": r["kind"],
                    "created_at": r["created_at"],
                    "timestamp_unix": r["timestamp_unix"],
                    "text": r["text"] or "",
                    "title": r["title"],
                    "target_author": r["target_author"],
                    "group_name": r["group_name"],
                    "url": r["url"],
                    "source": r["source"],
                    "media": media_map.get(r["activity_id"], []),
                }
                for r in rows
            ]
            return {"total": total, "offset": offset, "limit": limit, "items": items}

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

            conns_list = [dict(r) for r in conn_rows]
            resolved_avatar = next(
                (c["avatar_local_path"] for c in conns_list if c.get("avatar_local_path")),
                None,
            ) or self._resolve_avatar(conn, None, username=ident, full_name=ident)
            return {
                "identifier": ident,
                "avatar_local_path": resolved_avatar,
                "connections": conns_list,
                "comments": [dict(r) for r in comment_rows],
                "liked_posts": [dict(r) for r in liked_rows],
                "messages": [dict(r) for r in msg_rows],
            }
