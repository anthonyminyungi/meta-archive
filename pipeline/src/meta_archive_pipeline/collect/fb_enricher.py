"""Fast, resumable enrichment for Facebook friend avatars and shared link/post thumbnails."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from meta_archive_pipeline.collect.fb_collector import (
    _merge_shared_post_refs,
    _safe_filename_stem,
    extract_fb_friends_from_payloads,
    extract_fb_posts_from_payloads,
)
from meta_archive_pipeline.schema.models import Connection, Post, SharedPostRef
from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.fs import atomic_write_bytes, to_long_path
from meta_archive_pipeline.utils.media import verify_image

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_FB_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://www.facebook.com/",
}
_EXT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
_SKIP_OG_HOSTS = (
    "facebook.com",
    "fb.com",
    "fb.watch",
    "instagram.com",
    "m.facebook.com",
    "l.facebook.com",
)
_FORMAT_EXT = {
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "AVIF": ".avif",
}
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MAX_HTML_BYTES = 512 * 1024
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


@dataclass
class FbEnrichSummary:
    """Result counters for `enrich_facebook_collected`."""

    friends_total: int = 0
    friends_avatars_downloaded: int = 0
    friends_avatars_skipped_existing: int = 0
    friends_avatars_failed: int = 0
    posts_scanned: int = 0
    thumbs_from_raw_downloaded: int = 0
    thumbs_from_og_downloaded: int = 0
    thumbs_skipped_existing: int = 0
    thumbs_failed: int = 0


class _MetaTagParser(HTMLParser):
    """Lightweight HTML `<meta>` tag extractor for OpenGraph/Twitter preview cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
        key = (attr_map.get("property") or attr_map.get("name") or "").lower()
        content = attr_map.get("content") or ""
        if key and content and key not in self.meta:
            self.meta[key] = content


def _extract_youtube_thumb(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    vid: str | None = None
    if host in ("youtu.be", "www.youtu.be"):
        seg = parsed.path.strip("/").split("/")[0]
        if _YOUTUBE_ID_RE.match(seg):
            vid = seg
    elif host.endswith("youtube.com"):
        qs = urllib.parse.parse_qs(parsed.query)
        cand = (qs.get("v") or [""])[0]
        if _YOUTUBE_ID_RE.match(cand):
            vid = cand
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live") and _YOUTUBE_ID_RE.match(parts[1]):
                vid = parts[1]
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return None


def parse_og_metadata(html: str, base_url: str) -> tuple[str | None, str | None, str | None]:
    """Extract `(image_url, title, description)` from HTML OpenGraph / Twitter meta tags."""
    parser = _MetaTagParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    m = parser.meta
    raw_img = (
        m.get("og:image:secure_url")
        or m.get("og:image")
        or m.get("twitter:image")
        or m.get("twitter:image:src")
    )
    img_url: str | None = None
    if raw_img:
        joined = urllib.parse.urljoin(base_url, raw_img)
        if urllib.parse.urlparse(joined).scheme.lower() in ("http", "https"):
            img_url = joined
    if not img_url:
        img_url = _extract_youtube_thumb(base_url)

    title = m.get("og:title") or m.get("twitter:title") or None
    desc = m.get("og:description") or m.get("twitter:description") or m.get("description") or None
    return img_url, title, desc


def _is_safe_public_url(url: str, *, check_dns: bool = True) -> bool:
    """Ensure URL is http(s) and does not point at loopback/private/link-local hosts."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    low_host = host.lower().rstrip(".")
    if low_host in ("localhost", "localhost.localdomain") or low_host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(low_host)
        return ip.is_global
    except ValueError:
        pass
    if not check_dns:
        return True
    try:
        infos = socket.getaddrinfo(low_host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _is_skippable_external_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in _SKIP_OG_HOSTS)


def _existing_valid_media(collected_dir: Path, rel_path: str | None, fallback_glob_dir: Path, stem: str) -> Path | None:
    if rel_path:
        norm = normalize_rel_path(rel_path)
        parts = norm.split("/")
        candidates = [collected_dir.joinpath(*parts)]
        if len(parts) >= 3 and parts[0] == "media" and parts[1] in ("facebook", "instagram"):
            candidates.append(collected_dir.joinpath("media", *parts[2:]))
        for cand in candidates:
            if cand.exists() and verify_image(cand).ok:
                return cand
    if fallback_glob_dir.exists():
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
            cand = fallback_glob_dir / f"{stem}{ext}"
            if cand.exists() and verify_image(cand).ok:
                return cand
    return None


def _download_verified_image(
    client: httpx.Client,
    url: str,
    dest_no_ext: Path,
    *,
    headers: dict[str, str],
    min_dim: int = 16,
) -> Path | None:
    os.makedirs(to_long_path(dest_no_ext.parent), exist_ok=True)
    tmp_path: str | None = None
    try:
        with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return None
            total = 0
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=to_long_path(dest_no_ext.parent),
                prefix=f".{dest_no_ext.name[:80]}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
                for chunk in resp.iter_bytes(65536):
                    total += len(chunk)
                    if total > _MAX_IMAGE_BYTES:
                        return None
                    tmp.write(chunk)
                tmp.flush()
        check = verify_image(Path(tmp_path))
        if not check.ok:
            return None
        if (check.width or 0) < min_dim or (check.height or 0) < min_dim:
            return None
        ext = _FORMAT_EXT.get((check.format or "").upper(), ".jpg")
        final_dest = dest_no_ext.with_suffix(ext)
        os.replace(tmp_path, to_long_path(final_dest))
        tmp_path = None
        return final_dest
    except Exception:
        return None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _fetch_external_og_preview(
    client: httpx.Client,
    page_url: str,
    dest_no_ext: Path,
    *,
    check_dns: bool = True,
) -> tuple[Path | None, str | None, str | None]:
    if _is_skippable_external_host(page_url) or not _is_safe_public_url(page_url, check_dns=check_dns):
        return None, None, None

    yt_thumb = _extract_youtube_thumb(page_url)
    html_text = ""
    final_url = page_url
    try:
        with client.stream("GET", page_url, headers=_EXT_HEADERS, follow_redirects=True) as resp:
            if resp.status_code == 200:
                ctype = (resp.headers.get("content-type") or "").lower()
                if "html" in ctype or "xml" in ctype or not ctype:
                    chunks: list[bytes] = []
                    read_bytes = 0
                    for chunk in resp.iter_bytes(32768):
                        chunks.append(chunk)
                        read_bytes += len(chunk)
                        if read_bytes >= _MAX_HTML_BYTES:
                            break
                    raw = b"".join(chunks)
                    enc = resp.encoding or "utf-8"
                    html_text = raw.decode(enc, errors="replace")
                    final_url = str(resp.url)
    except Exception:
        html_text = ""

    img_url, title, desc = parse_og_metadata(html_text, final_url) if html_text else (yt_thumb, None, None)
    if not img_url or not _is_safe_public_url(img_url, check_dns=check_dns):
        return None, title, desc

    saved = _download_verified_image(
        client,
        img_url,
        dest_no_ext,
        headers={"User-Agent": _USER_AGENT, "Referer": final_url},
        min_dim=32,
    )
    return saved, title, desc


def _write_json_atomic(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def enrich_facebook_collected(
    collected_dir: Path,
    *,
    enrich_friends: bool = True,
    enrich_links: bool = True,
    fetch_external_og: bool = True,
    limit: int | None = None,
    timeout: float = 12.0,
    concurrency: int = 8,
    client: httpx.Client | None = None,
    check_dns: bool = True,
) -> FbEnrichSummary:
    """Hydrate missing Facebook friend avatars and shared link/post thumbnails from raw payloads and OG tags."""
    summary = FbEnrichSummary()
    owns_client = client is None
    http = client or httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
        limits=httpx.Limits(max_connections=max(concurrency * 2, 8), max_keepalive_connections=max(concurrency, 4)),
    )
    workers = max(1, concurrency)

    try:
        # 1. Hydrate Facebook friend avatars from raw/friends.json
        if enrich_friends:
            raw_friends_path = collected_dir / "raw" / "friends.json"
            friends_json_path = collected_dir / "friends.json"
            avatar_urls: dict[str, str] = {}
            extracted_conns: list[Connection] = []
            if raw_friends_path.exists():
                raw_payloads = json.loads(Path(to_long_path(raw_friends_path)).read_text(encoding="utf-8"))
                if isinstance(raw_payloads, list):
                    extracted_conns, avatar_urls = extract_fb_friends_from_payloads(raw_payloads)

            if friends_json_path.exists():
                raw_list = json.loads(Path(to_long_path(friends_json_path)).read_text(encoding="utf-8"))
                conns = [Connection.model_validate(item) for item in raw_list] if isinstance(raw_list, list) else []
            else:
                conns = extracted_conns

            summary.friends_total = len(conns)
            avatars_dir = collected_dir / "media" / "avatars"
            tasks: list[tuple[int, str, Path]] = []
            changed_friends = False

            for idx, conn in enumerate(conns):
                stem = _safe_filename_stem(conn.user_id or str(idx + 1))
                existing = _existing_valid_media(collected_dir, conn.avatar_local_path, avatars_dir, stem)
                if existing is not None:
                    rel = normalize_rel_path(f"media/facebook/avatars/{existing.name}")
                    if conn.avatar_local_path != rel:
                        conn.avatar_local_path = rel
                        changed_friends = True
                    summary.friends_avatars_skipped_existing += 1
                    continue

                key = conn.user_id or conn.profile_url or conn.full_name or ""
                url = avatar_urls.get(key)
                if not url:
                    continue
                if limit is not None and len(tasks) >= limit:
                    break
                tasks.append((idx, url, avatars_dir / stem))

            if tasks:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    fut_map = {
                        pool.submit(_download_verified_image, http, url, dest_no_ext, headers=_FB_HEADERS, min_dim=16): idx
                        for idx, url, dest_no_ext in tasks
                    }
                    for fut in as_completed(fut_map):
                        idx = fut_map[fut]
                        saved = fut.result()
                        if saved is not None:
                            conns[idx].avatar_local_path = normalize_rel_path(
                                f"media/facebook/avatars/{saved.name}"
                            )
                            summary.friends_avatars_downloaded += 1
                            changed_friends = True
                        else:
                            summary.friends_avatars_failed += 1

            if changed_friends or (conns and not friends_json_path.exists()):
                _write_json_atomic(friends_json_path, [c.model_dump(mode="json") for c in conns])

        # 2. Hydrate Facebook shared link / attached_story thumbnails from raw/posts/ + external OG fallback
        if enrich_links:
            raw_thumb_by_post: dict[str, str] = {}
            raw_shared_by_post: dict[str, SharedPostRef] = {}
            raw_posts_dir = collected_dir / "raw" / "posts"
            if raw_posts_dir.exists():
                for bf in sorted(raw_posts_dir.glob("batch_*.json")):
                    try:
                        payloads = json.loads(Path(to_long_path(bf)).read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if not isinstance(payloads, list):
                        continue
                    posts_extracted, cands_extracted = extract_fb_posts_from_payloads(payloads)
                    for ep in posts_extracted:
                        if ep.shared_post is not None:
                            raw_shared_by_post[ep.post_id] = _merge_shared_post_refs(
                                raw_shared_by_post.get(ep.post_id), ep.shared_post
                            ) or ep.shared_post
                    for cand in cands_extracted:
                        if cand.is_link_thumbnail and cand.progressive_url and cand.post_id not in raw_thumb_by_post:
                            raw_thumb_by_post[cand.post_id] = cand.progressive_url

            posts_dir = collected_dir / "posts"
            links_dir = collected_dir / "media" / "links"
            post_tasks: list[tuple[Path, Post, bool, str | None, Path]] = []

            if posts_dir.exists():
                for pf in sorted(posts_dir.glob("*.json")):
                    try:
                        post = Post.model_validate_json(Path(to_long_path(pf)).read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    summary.posts_scanned += 1
                    merged_sp = _merge_shared_post_refs(post.shared_post, raw_shared_by_post.get(post.post_id))
                    if merged_sp is None:
                        continue

                    meta_upgraded = merged_sp != post.shared_post
                    stem = f"{_safe_filename_stem(post.post_id)}_thumb"
                    existing = _existing_valid_media(collected_dir, merged_sp.thumbnail_local_path, links_dir, stem)
                    if existing is not None:
                        rel = normalize_rel_path(f"media/facebook/links/{existing.name}")
                        if merged_sp.thumbnail_local_path != rel:
                            merged_sp = merged_sp.model_copy(update={"thumbnail_local_path": rel})
                            meta_upgraded = True
                        if meta_upgraded:
                            updated = post.model_copy(update={"shared_post": merged_sp})
                            _write_json_atomic(pf, updated.model_dump(mode="json"))
                        summary.thumbs_skipped_existing += 1
                        continue

                    raw_url = raw_thumb_by_post.get(post.post_id)
                    if not raw_url and not (fetch_external_og and merged_sp.url and not _is_skippable_external_host(merged_sp.url)):
                        if meta_upgraded:
                            updated = post.model_copy(update={"shared_post": merged_sp})
                            _write_json_atomic(pf, updated.model_dump(mode="json"))
                        continue

                    if limit is not None and len(post_tasks) >= limit:
                        break
                    post_with_meta = post.model_copy(update={"shared_post": merged_sp}) if meta_upgraded else post
                    post_tasks.append((pf, post_with_meta, meta_upgraded, raw_url, links_dir / stem))

            def _run_thumb_task(
                pf: Path,
                post: Post,
                meta_upgraded: bool,
                raw_url: str | None,
                dest_no_ext: Path,
            ) -> tuple[Path, Post, bool, str | None, str | None, str | None, str]:
                # Returns (post_file, post, meta_upgraded, saved_filename, og_title, og_desc, source_tag)
                if raw_url:
                    saved = _download_verified_image(http, raw_url, dest_no_ext, headers=_FB_HEADERS, min_dim=16)
                    if saved is not None:
                        return pf, post, meta_upgraded, saved.name, None, None, "raw"
                if fetch_external_og and post.shared_post and post.shared_post.url:
                    saved, og_title, og_desc = _fetch_external_og_preview(
                        http, post.shared_post.url, dest_no_ext, check_dns=check_dns
                    )
                    if saved is not None:
                        return pf, post, meta_upgraded, saved.name, og_title, og_desc, "og"
                    if og_title or og_desc:
                        return pf, post, meta_upgraded, None, og_title, og_desc, "failed"
                return pf, post, meta_upgraded, None, None, None, "failed"

            if post_tasks:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = [
                        pool.submit(_run_thumb_task, pf, post, meta_upgraded, raw_url, dest_no_ext)
                        for pf, post, meta_upgraded, raw_url, dest_no_ext in post_tasks
                    ]
                    for fut in as_completed(futs):
                        pf, post, meta_upgraded, saved_name, og_title, og_desc, source_tag = fut.result()
                        sp = post.shared_post
                        if sp is None:
                            continue
                        updates: dict[str, Any] = {}
                        if saved_name:
                            updates["thumbnail_local_path"] = normalize_rel_path(
                                f"media/facebook/links/{saved_name}"
                            )
                            if source_tag == "raw":
                                summary.thumbs_from_raw_downloaded += 1
                            else:
                                summary.thumbs_from_og_downloaded += 1
                        else:
                            summary.thumbs_failed += 1
                        if og_title and not sp.title:
                            updates["title"] = og_title
                        if og_desc and not sp.text:
                            updates["text"] = og_desc
                        if updates or meta_upgraded:
                            next_sp = sp.model_copy(update=updates) if updates else sp
                            updated = post.model_copy(update={"shared_post": next_sp})
                            _write_json_atomic(pf, updated.model_dump(mode="json"))
    finally:
        if owns_client:
            http.close()

    return summary
