"""FastAPI read-only application for Meta Archive viewer and HTTP Range media streaming."""

from __future__ import annotations

import mimetypes
import os
import re
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from meta_archive_viewer import __version__
from meta_archive_viewer.queries import ArchiveNotReadyError, ArchiveReader, SchemaVersionError

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("font/woff2", ".woff2")


def _sniff_magic_mime(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        return "video/mp4"
    return None


def _guess_media_mime(path: Path) -> str:
    """Return MIME type, sniffing actual magic bytes for `.avif` and extensionless/unknown files."""
    ext = path.suffix.lower()
    if ext == ".avif":
        return _sniff_magic_mime(path) or "image/avif"
    mime, _ = mimetypes.guess_type(path.name)
    if not mime or mime == "application/octet-stream":
        return _sniff_magic_mime(path) or "application/octet-stream"
    return mime

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _has_unsafe_segments(raw_path: str) -> bool:
    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        return True
    if raw_path.startswith("/"):
        return True
    parts = [p for p in raw_path.split("/") if p]
    if not parts:
        return True
    if any(part in (".", "..") for part in parts):
        return True
    if ":" in parts[0]:
        return True
    return False


def _resolve_media_file(archive_dir: Path, rel_path: str) -> Path:
    decoded = urllib.parse.unquote(rel_path)
    if _has_unsafe_segments(rel_path) or _has_unsafe_segments(decoded):
        raise HTTPException(status_code=400, detail="Invalid media path")

    normalized = decoded
    if normalized.startswith("media/"):
        normalized = normalized[len("media/") :]
    if _has_unsafe_segments(normalized):
        raise HTTPException(status_code=400, detail="Invalid media path")

    media_root = (archive_dir / "media").resolve()
    candidate = (media_root / normalized).resolve()
    if candidate == media_root or not candidate.is_relative_to(media_root):
        raise HTTPException(status_code=400, detail="Invalid media path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return candidate


def _parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    match = _RANGE_RE.match(range_header.strip())
    if not match:
        raise ValueError("Malformed Range header")
    start_s, end_s = match.groups()
    if not start_s and not end_s:
        raise ValueError("Empty Range bounds")

    if file_size <= 0:
        raise ValueError("Empty file cannot satisfy byte range")

    if start_s == "":
        suffix_len = int(end_s)
        if suffix_len <= 0:
            raise ValueError("Zero suffix range")
        start = max(0, file_size - suffix_len)
        end = file_size - 1
        return start, end

    start = int(start_s)
    end = file_size - 1 if end_s == "" else int(end_s)
    if start < 0 or start >= file_size or end < start:
        raise ValueError("Range out of bounds")
    end = min(end, file_size - 1)
    return start, end


def _iter_file_range(path: Path, start: int, end: int, chunk_size: int = 65536) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def create_app(archive_dir: Path | None = None) -> FastAPI:
    """Create configured read-only FastAPI app for a Meta Archive directory."""
    resolved_archive = Path(
        archive_dir if archive_dir is not None else os.environ.get("ARCHIVE_DIR", "/data/archive")
    )
    reader = ArchiveReader(resolved_archive)

    fastapi_app = FastAPI(
        title="Meta Archive Viewer",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    fastapi_app.state.archive_dir = resolved_archive
    fastapi_app.state.reader = reader

    @fastapi_app.middleware("http")
    async def _static_cache_middleware(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/fonts/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @fastapi_app.exception_handler(ArchiveNotReadyError)
    async def _not_ready_handler(_req: Request, exc: ArchiveNotReadyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @fastapi_app.exception_handler(SchemaVersionError)
    async def _schema_error_handler(_req: Request, exc: SchemaVersionError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @fastapi_app.get("/api/health")
    def api_health() -> dict[str, Any]:
        return reader.check_health()

    @fastapi_app.get("/api/overview")
    def api_overview() -> dict[str, Any]:
        return reader.get_overview()

    @fastapi_app.get("/api/profiles")
    def api_profiles(platform: str | None = None) -> dict[str, Any]:
        return {"items": reader.get_profiles(platform=platform)}

    @fastapi_app.get("/api/posts")
    def api_posts(
        platform: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=200),
        year: int | None = None,
        month: int | None = Query(default=None, ge=1, le=12),
        archived: bool | None = None,
        post_type: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        return reader.list_posts(
            platform=platform,
            offset=offset,
            limit=limit,
            year=year,
            month=month,
            archived=archived,
            post_type=post_type,
            album=album,
        )

    @fastapi_app.get("/api/posts/{post_id:path}")
    def api_post_detail(post_id: str) -> dict[str, Any]:
        detail = reader.get_post_detail(post_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Post not found")
        return detail

    @fastapi_app.get("/api/highlights")
    def api_highlights(platform: str = "instagram") -> dict[str, Any]:
        return {"items": reader.list_highlights(platform=platform)}

    @fastapi_app.get("/api/stories")
    def api_stories(
        platform: str = "instagram",
        highlight_id: str | None = None,
        story_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return reader.list_stories(
            platform=platform,
            highlight_id=highlight_id,
            story_id=story_id,
            offset=offset,
            limit=limit,
        )

    @fastapi_app.get("/api/activities")
    def api_activities(
        platform: str | None = None,
        kind: str | None = None,
        q: str | None = None,
        activity_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return reader.list_activities(
            platform=platform,
            kind=kind,
            q=q,
            activity_id=activity_id,
            offset=offset,
            limit=limit,
        )

    @fastapi_app.get("/api/connections")
    def api_connections(
        platform: str | None = None,
        relation: str | None = None,
        q: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return reader.list_connections(
            platform=platform,
            relation=relation,
            q=q,
            offset=offset,
            limit=limit,
        )

    @fastapi_app.get("/api/albums")
    def api_albums(platform: str = "facebook") -> dict[str, Any]:
        return {"items": reader.list_albums(platform=platform)}

    @fastapi_app.get("/api/timeline")
    def api_timeline(platform: str = "facebook") -> dict[str, Any]:
        return {"items": reader.list_timeline_months(platform=platform)}

    @fastapi_app.get("/api/threads")
    def api_threads(
        platform: str | None = None,
        q: str | None = None,
        sort: str = "recent",
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return reader.list_threads(platform=platform, q=q, sort=sort, offset=offset, limit=limit)

    @fastapi_app.get("/api/threads/{thread_id:path}/messages")
    def api_thread_messages(
        thread_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        tail: bool = False,
        before_seq: int | None = None,
        around_ts: int | None = None,
        q: str | None = None,
        media_only: bool = False,
    ) -> dict[str, Any]:
        data = reader.get_thread_messages(
            thread_id,
            offset=offset,
            limit=limit,
            tail=tail,
            before_seq=before_seq,
            around_ts=around_ts,
            q=q,
            media_only=media_only,
        )
        if data is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return data

    @fastapi_app.get("/api/search")
    def api_search(
        q: str = "",
        platform: str | None = None,
        entity_type: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        return reader.search(
            query=q,
            platform=platform,
            entity_type=entity_type,
            limit=limit,
        )

    @fastapi_app.get("/api/people/{identifier:path}")
    def api_person(
        identifier: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        return reader.get_person_activity(identifier=identifier, limit=limit)

    @fastapi_app.get("/api/memories/on-this-day")
    def api_memories_on_this_day(
        month: int | None = Query(default=None, ge=1, le=12),
        day: int | None = Query(default=None, ge=1, le=31),
        include_shared: bool = False,
        random_day: bool = False,
    ) -> dict[str, Any]:
        return reader.get_memories_on_this_day(
            month=month,
            day=day,
            include_shared=include_shared,
            random_day=random_day,
        )

    @fastapi_app.get("/api/memories/yearbook")
    def api_memories_yearbook(
        year: int | None = Query(default=None, ge=2000, le=2100),
    ) -> dict[str, Any]:
        return reader.get_memories_yearbook(year=year)

    @fastapi_app.get("/api/memories/people")
    def api_memories_people(
        limit: int = Query(default=24, ge=1, le=100),
    ) -> dict[str, Any]:
        return reader.get_memories_inner_circle(limit=limit)

    @fastapi_app.get("/api/memories/photos")
    def api_memories_photos(
        year: int | None = Query(default=None, ge=2000, le=2100),
        platform: str | None = None,
        place: str | None = None,
        sort: str = "recent",
        media_ids: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=60, ge=1, le=200),
    ) -> dict[str, Any]:
        parsed_ids: list[int] | None = None
        if media_ids is not None:
            parsed_ids = [int(tok) for tok in media_ids.split(",") if tok.strip().isdigit()]
        return reader.list_memories_photos(
            year=year,
            platform=platform,
            place=place,
            sort=sort,
            media_ids=parsed_ids,
            offset=offset,
            limit=limit,
        )

    @fastapi_app.api_route("/media/{rel_path:path}", methods=["GET", "HEAD"])
    def serve_media(rel_path: str, request: Request) -> Response:
        target = _resolve_media_file(resolved_archive, rel_path)
        file_size = target.stat().st_size
        media_type = _guess_media_mime(target)
        base_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=31536000, immutable",
        }

        range_header = request.headers.get("range")
        if not range_header:
            return FileResponse(
                target,
                media_type=media_type,
                headers=base_headers,
            )

        try:
            start, end = _parse_range_header(range_header, file_size)
        except ValueError:
            return Response(
                status_code=416,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes */{file_size}",
                },
            )

        content_length = end - start + 1
        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
        }
        if request.method == "HEAD":
            return Response(status_code=206, headers=headers, media_type=media_type)
        return StreamingResponse(
            _iter_file_range(target, start, end),
            status_code=206,
            headers=headers,
            media_type=media_type,
        )

    if _STATIC_DIR.is_dir():
        fastapi_app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

        @fastapi_app.get("/")
        def index() -> FileResponse:
            return FileResponse(_STATIC_DIR / "index.html")

    return fastapi_app


app = create_app()
