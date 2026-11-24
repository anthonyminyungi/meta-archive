"""Instagram CDP collector and pure SSR/GraphQL/v1/DASH payload parsers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from meta_archive_pipeline.collect.cdp import (
    DEFAULT_CDP_HOST,
    DEFAULT_CDP_PORT,
    CooldownTracker,
    ResponseCollector,
    SafetyStopError,
    check_page_safety,
    connect_cdp,
    extract_ssr_json_payloads,
)
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    Location,
    MediaItem,
    Post,
    Profile,
    ProfileCounts,
    Story,
    UserRef,
)
from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.checkpoint import CheckpointStore
from meta_archive_pipeline.utils.fs import atomic_write_bytes, sha256_file, to_long_path
from meta_archive_pipeline.utils.media import (
    download_media_via_context,
    inspect_mp4,
    mux_dash_streams,
    set_file_mtime,
    verify_image,
)

IgPass = Literal["core", "likes", "connections", "tagged", "saved"]

_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CODE_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
_DOM_HREF_SHORTCODE_RE = re.compile(
    r"^/(?:([A-Za-z0-9._]+)/)?(?:p|reel|tv)/([A-Za-z0-9_-]{5,})(?:[/?#]|$)"
)
_DOM_HIGHLIGHT_HREF_RE = re.compile(r"/stories/highlights/(\d+)/?")
_RESERVED_IG_SEGMENTS = frozenset({"explore", "accounts", "direct", "stories", "reels"})
_EXCLUDED_SHORTCODE_CONTAINERS = frozenset(
    {
        "xdt_api__v1__feed__timeline__connection",
        "xdt_api__v1__discover__web__explore_grid__connection",
        "explore_story",
        "ad",
        "injected",
        "chaining_results",
        "suggested_users",
        "carousel_media",
        "edge_sidecar_to_children",
    }
)
_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")
_EXPAND_BUTTON_LABELS: tuple[Any, ...] = (
    "숨겨진 댓글 보기",
    "댓글 더 보기",
    "댓글 더 읽어들이기",
    "답글 보기",
    "답글 모두 보기",
    "View hidden comments",
    "View more comments",
    "Load more comments",
    "View replies",
    re.compile(r"(?:답글.*보기|View (?:all \d+ |more )?replies)", re.IGNORECASE),
)


@dataclass(frozen=True)
class IgMediaDownloadCandidate:
    """Download URLs and metadata for a single Instagram image or video/reel."""

    media_id: str | None
    media_type: Literal["image", "video", "audio", "unknown"]
    progressive_url: str | None = None
    dash_video_url: str | None = None
    dash_audio_url: str | None = None
    width: int | None = None
    height: int | None = None
    is_carousel_cover: bool = False


@dataclass
class IgCollectConfig:
    """Configuration for an Instagram CDP collection pass."""

    username: str
    output_dir: Path
    cdp_url: str = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}"
    collect_pass: IgPass = "core"
    limit: int | None = None
    shortcodes: list[str] | None = None
    download_avatars: bool = True
    cooldown: CooldownTracker = field(default_factory=CooldownTracker)
    max_expand_clicks: int = 30
    max_scrolls: int = 120


@dataclass
class IgCollectSummary:
    """Result metrics for an Instagram collection pass."""

    collect_pass: IgPass
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_done: int = 0
    paused_reason: str | None = None


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_dash_manifest(xml_text: str) -> tuple[str | None, str | None]:
    """Parse an MPEG-DASH MPD XML manifest and return `(best_video_url, best_audio_url)`."""
    if not xml_text or not xml_text.strip():
        return None, None
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return None, None

    best_video: tuple[int, str] | None = None
    best_audio: tuple[int, str] | None = None

    for adapt in root.iter():
        if _local_tag(adapt.tag) != "AdaptationSet":
            continue
        adapt_mime = (adapt.attrib.get("mimeType") or "").lower()
        adapt_content = (adapt.attrib.get("contentType") or "").lower()

        for child in adapt:
            if _local_tag(child.tag) != "Representation":
                continue
            mime = (child.attrib.get("mimeType") or adapt_mime).lower()
            try:
                bandwidth = int(child.attrib.get("bandwidth") or 0)
            except ValueError:
                bandwidth = 0

            base_url: str | None = None
            for sub in child:
                if _local_tag(sub.tag) == "BaseURL" and sub.text:
                    base_url = sub.text.strip()
                    break
            if not base_url:
                continue

            is_video = mime.startswith("video/") or (not mime and adapt_content == "video")
            is_audio = mime.startswith("audio/") or (not mime and adapt_content == "audio")
            if is_video and (best_video is None or bandwidth > best_video[0]):
                best_video = (bandwidth, base_url)
            elif is_audio and (best_audio is None or bandwidth > best_audio[0]):
                best_audio = (bandwidth, base_url)

    return (
        best_video[1] if best_video else None,
        best_audio[1] if best_audio else None,
    )


def _walk_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for val in obj.values():
            yield from _walk_dicts(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _safe_int(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str) and val.isdigit():
        return int(val)
    return None


def _safe_float(val: Any) -> float | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _ts_to_dt(ts: int | None) -> datetime | None:
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _extract_edge_nodes(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, dict):
        return []
    edges = container.get("edges")
    if not isinstance(edges, list):
        return []
    nodes: list[dict[str, Any]] = []
    for edge in edges:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            nodes.append(edge["node"])
    return nodes


def extract_ig_profile_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    target_username: str | None = None,
) -> tuple[Profile | None, str | None]:
    """Extract the richest Instagram Profile and avatar URL from SSR/API payloads."""
    target_norm = target_username.strip().lower() if target_username else None
    fragments_by_user: dict[str, list[tuple[int, dict[str, Any]]]] = {}

    for payload in payloads:
        for node in _walk_dicts(payload):
            username = node.get("username")
            if not isinstance(username, str) or not username:
                continue
            uname_low = username.strip().lower()
            if target_norm and uname_low != target_norm:
                continue

            has_gql = any(
                k in node
                for k in ("edge_owner_to_timeline_media", "edge_followed_by", "edge_follow")
            )
            has_v1 = any(
                k in node for k in ("media_count", "follower_count", "following_count")
            )
            has_bio = "biography" in node
            if not (has_gql or has_v1 or has_bio):
                continue

            posts_cnt = _safe_int(
                (node.get("edge_owner_to_timeline_media") or {}).get("count")
                if isinstance(node.get("edge_owner_to_timeline_media"), dict)
                else None
            )
            if posts_cnt is None:
                posts_cnt = _safe_int(node.get("media_count"))

            followers_cnt = _safe_int(
                (node.get("edge_followed_by") or {}).get("count")
                if isinstance(node.get("edge_followed_by"), dict)
                else None
            )
            if followers_cnt is None:
                followers_cnt = _safe_int(node.get("follower_count"))

            following_cnt = _safe_int(
                (node.get("edge_follow") or {}).get("count")
                if isinstance(node.get("edge_follow"), dict)
                else None
            )
            if following_cnt is None:
                following_cnt = _safe_int(node.get("following_count"))

            avatar_url = node.get("profile_pic_url_hd") or node.get("profile_pic_url")
            user_id = str(node.get("id") or node.get("pk") or "") or None
            full_name = node.get("full_name") if isinstance(node.get("full_name"), str) else None
            bio = node.get("biography") if isinstance(node.get("biography"), str) else None

            parsed_frag = {
                "username": username,
                "user_id": user_id,
                "full_name": full_name,
                "bio": bio,
                "avatar_url": avatar_url if isinstance(avatar_url, str) else None,
                "posts_cnt": posts_cnt,
                "followers_cnt": followers_cnt,
                "following_cnt": following_cnt,
            }
            score = sum(v is not None for k, v in parsed_frag.items() if k != "username")
            fragments_by_user.setdefault(uname_low, []).append((score, parsed_frag))

    if not fragments_by_user:
        return None, None

    best_score = -1
    best_profile: Profile | None = None
    best_avatar: str | None = None

    for frags in fragments_by_user.values():
        frags_sorted = [f for _, f in sorted(frags, key=lambda item: item[0], reverse=True)]
        merged: dict[str, Any] = {}
        for field_name in (
            "username",
            "user_id",
            "full_name",
            "bio",
            "avatar_url",
            "posts_cnt",
            "followers_cnt",
            "following_cnt",
        ):
            merged[field_name] = next(
                (f[field_name] for f in frags_sorted if f[field_name] is not None),
                None,
            )
        total_score = sum(v is not None for k, v in merged.items() if k != "username")
        if total_score > best_score:
            best_score = total_score
            best_avatar = merged["avatar_url"]
            best_profile = Profile(
                platform="instagram",
                user_id=merged["user_id"],
                username=merged["username"],
                full_name=merged["full_name"],
                bio=merged["bio"],
                counts=ProfileCounts(
                    posts=merged["posts_cnt"],
                    followers=merged["followers_cnt"],
                    following=merged["following_cnt"],
                ),
            )

    return best_profile, best_avatar


def _looks_like_media_dict(d: dict[str, Any]) -> bool:
    return any(
        k in d
        for k in (
            "pk",
            "id",
            "taken_at",
            "taken_at_timestamp",
            "media_type",
            "image_versions2",
            "carousel_media",
            "video_versions",
            "display_url",
            "edge_sidecar_to_children",
            "__typename",
        )
    )


def _walk_shortcode_dicts(
    obj: Any,
    inherited_owner: str | None = None,
) -> Iterator[tuple[dict[str, Any], str | None]]:
    if isinstance(obj, dict):
        uname = obj.get("username")
        if isinstance(uname, str) and uname and any(
            k in obj
            for k in (
                "edge_owner_to_timeline_media",
                "media_count",
                "edge_followed_by",
                "follower_count",
            )
        ):
            inherited_owner = uname.strip().lower()
        yield obj, inherited_owner
        for key, val in obj.items():
            if key in _EXCLUDED_SHORTCODE_CONTAINERS:
                continue
            yield from _walk_shortcode_dicts(val, inherited_owner)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_shortcode_dicts(item, inherited_owner)


def _node_explicit_authors(d: dict[str, Any]) -> set[str]:
    authors: set[str] = set()
    for key in ("owner", "user"):
        u = d.get(key)
        if isinstance(u, dict) and isinstance(u.get("username"), str) and u["username"]:
            authors.add(u["username"].strip().lower())
    coauthors = d.get("coauthor_producers")
    if isinstance(coauthors, list):
        for entry in coauthors:
            if isinstance(entry, dict) and isinstance(entry.get("username"), str) and entry["username"]:
                authors.add(entry["username"].strip().lower())
    return authors


def extract_ig_dom_shortcodes(hrefs: Sequence[str]) -> list[str]:
    """Extract ordered post/reel shortcodes from `<main>` anchor `href` attributes."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_href in hrefs:
        if not isinstance(raw_href, str) or not raw_href:
            continue
        m = _DOM_HREF_SHORTCODE_RE.match(raw_href.strip())
        if not m:
            continue
        prefix, sc = m.group(1), m.group(2)
        if prefix and prefix.lower() in _RESERVED_IG_SEGMENTS:
            continue
        if sc not in seen:
            seen.add(sc)
            ordered.append(sc)
    return ordered


def extract_ig_shortcodes_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    owner_username: str | None = None,
) -> list[str]:
    """Extract unique ordered post/reel shortcodes while excluding Home Feed/Explore/Ad preloads."""
    target = owner_username.strip().lower() if owner_username else None
    raw_entries: list[tuple[str, str | None]] = []
    known_authors_by_code: dict[str, set[str]] = {}

    for payload in payloads:
        for d, inherited_owner in _walk_shortcode_dicts(payload):
            if d.get("carousel_parent_id"):
                continue
            if (
                "campaign_id" in d
                or "dl_source" in d
                or any(k.startswith("__relay_internal__") for k in d)
            ):
                continue

            raw_shortcode = d.get("shortcode")
            if isinstance(raw_shortcode, str) and _SHORTCODE_RE.match(raw_shortcode):
                code = raw_shortcode
            else:
                raw_code = d.get("code")
                if (
                    isinstance(raw_code, str)
                    and _CODE_SHORTCODE_RE.match(raw_code)
                    and _looks_like_media_dict(d)
                ):
                    code = raw_code
                else:
                    continue

            explicit_authors = _node_explicit_authors(d)
            if explicit_authors:
                known_authors_by_code.setdefault(code, set()).update(explicit_authors)
            raw_entries.append((code, inherited_owner))

    seen: set[str] = set()
    ordered: list[str] = []
    for code, inherited_owner in raw_entries:
        if code in seen:
            continue
        if target:
            known_authors = known_authors_by_code.get(code)
            if known_authors:
                if target not in known_authors:
                    continue
            elif inherited_owner and inherited_owner != target:
                continue
        seen.add(code)
        ordered.append(code)

    return ordered


def _best_image_candidate(node: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    img_v2 = node.get("image_versions2")
    if isinstance(img_v2, dict) and isinstance(img_v2.get("candidates"), list):
        best_url = None
        best_area = -1
        best_dims: tuple[int | None, int | None] = (None, None)
        for cand in img_v2["candidates"]:
            if not isinstance(cand, dict) or not isinstance(cand.get("url"), str):
                continue
            w = _safe_int(cand.get("width"))
            h = _safe_int(cand.get("height"))
            area = (w or 0) * (h or 0)
            if area > best_area:
                best_area = area
                best_url = cand["url"]
                best_dims = (w, h)
        if best_url:
            return best_url, best_dims[0], best_dims[1]

    resources = node.get("display_resources")
    if isinstance(resources, list) and resources:
        best_url = None
        best_area = -1
        best_dims = (None, None)
        for res in resources:
            if not isinstance(res, dict) or not isinstance(res.get("src"), str):
                continue
            w = _safe_int(res.get("config_width"))
            h = _safe_int(res.get("config_height"))
            area = (w or 0) * (h or 0)
            if area > best_area:
                best_area = area
                best_url = res["src"]
                best_dims = (w, h)
        if best_url:
            return best_url, best_dims[0], best_dims[1]

    display_url = node.get("display_url")
    if isinstance(display_url, str) and display_url:
        dims = node.get("dimensions") if isinstance(node.get("dimensions"), dict) else {}
        return (
            display_url,
            _safe_int(dims.get("width")) or _safe_int(node.get("original_width")),
            _safe_int(dims.get("height")) or _safe_int(node.get("original_height")),
        )

    return None, None, None


def _media_candidate_from_node(
    node: dict[str, Any],
    *,
    is_carousel_cover: bool = False,
) -> IgMediaDownloadCandidate:
    media_id = str(node.get("id") or node.get("pk") or "") or None
    is_video = bool(
        node.get("is_video")
        or node.get("media_type") == 2
        or node.get("video_versions")
        or node.get("video_url")
    )

    if is_video:
        prog_url: str | None = None
        best_w: int | None = None
        best_h: int | None = None
        best_area = -1

        if isinstance(node.get("video_versions"), list):
            for ver in node["video_versions"]:
                if not isinstance(ver, dict) or not isinstance(ver.get("url"), str):
                    continue
                w = _safe_int(ver.get("width"))
                h = _safe_int(ver.get("height"))
                area = (w or 0) * (h or 0)
                if area > best_area:
                    best_area = area
                    prog_url = ver["url"]
                    best_w, best_h = w, h

        if not prog_url and isinstance(node.get("video_url"), str):
            prog_url = node["video_url"]

        dash_xml = node.get("video_dash_manifest")
        if not dash_xml and isinstance(node.get("dash_info"), dict):
            dash_xml = node["dash_info"].get("video_dash_manifest")
        dash_v, dash_a = parse_dash_manifest(dash_xml) if isinstance(dash_xml, str) else (None, None)

        if best_w is None:
            _, best_w, best_h = _best_image_candidate(node)

        return IgMediaDownloadCandidate(
            media_id=media_id,
            media_type="video",
            progressive_url=prog_url,
            dash_video_url=dash_v,
            dash_audio_url=dash_a,
            width=best_w,
            height=best_h,
            is_carousel_cover=is_carousel_cover,
        )

    img_url, w, h = _best_image_candidate(node)
    return IgMediaDownloadCandidate(
        media_id=media_id,
        media_type="image",
        progressive_url=img_url,
        width=w,
        height=h,
        is_carousel_cover=is_carousel_cover,
    )


def _user_ref_from_dict(d: Any) -> UserRef:
    if not isinstance(d, dict):
        return UserRef()
    username = d.get("username") if isinstance(d.get("username"), str) else None
    full_name = d.get("full_name") if isinstance(d.get("full_name"), str) else None
    uid = str(d.get("id") or d.get("pk") or "") or None
    profile_url = f"https://www.instagram.com/{username}/" if username else None
    return UserRef(
        user_id=uid,
        username=username,
        full_name=full_name,
        profile_url=profile_url,
    )


def _parse_ig_comment_node(node: dict[str, Any]) -> Comment:
    cid = str(node.get("id") or node.get("pk") or "") or None
    owner = node.get("owner") if isinstance(node.get("owner"), dict) else node.get("user")
    author = _user_ref_from_dict(owner)
    text = node.get("text") if isinstance(node.get("text"), str) else ""
    ts = _safe_int(node.get("created_at") or node.get("created_at_utc"))
    like_cnt = _safe_int(node.get("comment_like_count"))
    if like_cnt is None and isinstance(node.get("edge_liked_by"), dict):
        like_cnt = _safe_int(node["edge_liked_by"].get("count"))

    raw_replies: list[dict[str, Any]] = []
    raw_replies.extend(_extract_edge_nodes(node.get("edge_threaded_comments")))
    for key in ("child_comments", "preview_child_comments"):
        if isinstance(node.get(key), list):
            raw_replies.extend(c for c in node[key] if isinstance(c, dict))

    replies = _merge_comments([_parse_ig_comment_node(r) for r in raw_replies])
    return Comment(
        comment_id=cid,
        author=author,
        text=text,
        created_at=_ts_to_dt(ts),
        timestamp_unix=ts,
        like_count=like_cnt,
        replies=replies,
    )


def _comment_dedup_key(c: Comment) -> tuple[str, ...]:
    if c.comment_id:
        return ("id", c.comment_id)
    return ("fallback", c.author.username or "", str(c.timestamp_unix or ""), c.text)


def _merge_two_comments(existing: Comment, incoming: Comment) -> Comment:
    merged_replies = _merge_comments([*existing.replies, *incoming.replies])
    author = UserRef(
        user_id=existing.author.user_id or incoming.author.user_id,
        username=existing.author.username or incoming.author.username,
        full_name=existing.author.full_name or incoming.author.full_name,
        profile_url=existing.author.profile_url or incoming.author.profile_url,
        avatar_local_path=existing.author.avatar_local_path or incoming.author.avatar_local_path,
    )
    return Comment(
        comment_id=existing.comment_id or incoming.comment_id,
        author=author,
        text=existing.text or incoming.text,
        created_at=existing.created_at or incoming.created_at,
        timestamp_unix=existing.timestamp_unix or incoming.timestamp_unix,
        like_count=max(
            [v for v in (existing.like_count, incoming.like_count) if v is not None],
            default=None,
        ),
        replies=merged_replies,
    )


def _merge_comments(comments: Sequence[Comment]) -> list[Comment]:
    by_key: dict[tuple[str, ...], Comment] = {}
    for c in comments:
        key = _comment_dedup_key(c)
        if key in by_key:
            by_key[key] = _merge_two_comments(by_key[key], c)
        else:
            by_key[key] = c

    result = list(by_key.values())
    result.sort(key=lambda c: c.timestamp_unix if c.timestamp_unix is not None else 0)
    return result


def _collect_comments_from_media_node(node: dict[str, Any]) -> list[Comment]:
    raw: list[dict[str, Any]] = []
    for edge_key in ("edge_media_to_parent_comment", "edge_media_to_comment"):
        raw.extend(_extract_edge_nodes(node.get(edge_key)))
    for list_key in ("comments", "preview_comments"):
        if isinstance(node.get(list_key), list):
            raw.extend(c for c in node[list_key] if isinstance(c, dict))
    return [_parse_ig_comment_node(c) for c in raw]


def _walk_comment_containers(
    obj: Any,
    target_media_id: str | None,
) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        polaris_id = obj.get("id")
        if (
            target_media_id
            and isinstance(polaris_id, str)
            and polaris_id.startswith("POLARIS_")
            and polaris_id != f"POLARIS_{target_media_id}"
        ):
            return
        yield obj
        for val in obj.values():
            yield from _walk_comment_containers(val, target_media_id)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_comment_containers(item, target_media_id)


def _collect_paginated_comments(
    payloads: Sequence[dict[str, Any]],
    target_media_id: str | None,
) -> tuple[list[Comment], dict[str, list[Comment]]]:
    top_level: list[Comment] = []
    replies_by_parent: dict[str, list[Comment]] = {}

    def _record_raw_comment(c: dict[str, Any]) -> None:
        parsed_c = _parse_ig_comment_node(c)
        parent_id = str(c.get("parent_comment_id") or "")
        if parent_id:
            replies_by_parent.setdefault(parent_id, []).append(parsed_c)
        else:
            top_level.append(parsed_c)

    for payload in payloads:
        for d in _walk_comment_containers(payload, target_media_id):
            # v1 comments endpoint: {"comments": [...], ...}
            if isinstance(d.get("comments"), list) and "shortcode" not in d and "code" not in d:
                for c in d["comments"]:
                    if isinstance(c, dict) and ("text" in c or "pk" in c):
                        media_id_attr = str(c.get("media_id") or "")
                        if target_media_id and media_id_attr and media_id_attr != target_media_id:
                            continue
                        _record_raw_comment(c)

            # GraphQL / 2026 Polaris comments connections
            for key, val in d.items():
                if (
                    key in ("comments_connection", "child_comments_connection", "edge_media_to_parent_comment")
                    or "comments__connection" in key
                ):
                    for c in _extract_edge_nodes(val):
                        _record_raw_comment(c)
    return top_level, replies_by_parent


def _post_belongs_to_user(
    shortcode: str,
    post: Post,
    payloads: Sequence[dict[str, Any]],
    owner_username: str,
) -> bool:
    target = owner_username.strip().lower()
    if post.author.username and post.author.username.strip().lower() == target:
        return True
    explicit_authors: set[str] = set()
    if post.author.username:
        explicit_authors.add(post.author.username.strip().lower())
    for payload in payloads:
        for d in _walk_dicts(payload):
            if d.get("shortcode") == shortcode or d.get("code") == shortcode:
                explicit_authors.update(_node_explicit_authors(d))
    if not explicit_authors:
        return True
    return target in explicit_authors


def extract_ig_post_from_payloads(
    shortcode: str,
    payloads: Sequence[dict[str, Any]],
) -> tuple[Post | None, list[IgMediaDownloadCandidate]]:
    """Extract normalized Post and media download candidates for a shortcode."""
    matching_nodes: list[dict[str, Any]] = []
    for payload in payloads:
        for d in _walk_dicts(payload):
            if d.get("shortcode") == shortcode or d.get("code") == shortcode:
                if _looks_like_media_dict(d):
                    matching_nodes.append(d)

    if not matching_nodes:
        return None, []

    def _richness(n: dict[str, Any]) -> int:
        return sum(
            bool(n.get(k))
            for k in (
                "edge_sidecar_to_children",
                "carousel_media",
                "edge_media_to_parent_comment",
                "edge_media_to_comment",
                "comments",
                "video_versions",
                "video_url",
                "image_versions2",
                "display_url",
                "location",
                "edge_media_to_tagged_user",
                "usertags",
            )
        )

    primary = max(matching_nodes, key=_richness)
    post_id = str(primary.get("id") or primary.get("pk") or shortcode)
    raw_media_id = str(primary.get("pk") or primary.get("id") or "")
    pk_id = raw_media_id.split("_", 1)[0] if raw_media_id else None

    product_type = primary.get("product_type")
    typename = str(primary.get("__typename") or "")
    is_reel = product_type in ("clips", "reels") or "Reel" in typename
    path_kind = "reel" if is_reel else "p"
    url = f"https://www.instagram.com/{path_kind}/{shortcode}/"

    owner = primary.get("owner") if isinstance(primary.get("owner"), dict) else primary.get("user")
    author = _user_ref_from_dict(owner)

    ts = _safe_int(primary.get("taken_at_timestamp") or primary.get("taken_at"))
    created_at = _ts_to_dt(ts)

    caption = ""
    cap_edges = _extract_edge_nodes(primary.get("edge_media_to_caption"))
    if cap_edges and isinstance(cap_edges[0].get("text"), str):
        caption = cap_edges[0]["text"]
    elif isinstance(primary.get("caption"), dict) and isinstance(primary["caption"].get("text"), str):
        caption = primary["caption"]["text"]
    elif isinstance(primary.get("caption"), str):
        caption = primary["caption"]

    hashtags = list(dict.fromkeys(_HASHTAG_RE.findall(caption)))

    location: Location | None = None
    if isinstance(primary.get("location"), dict):
        loc = primary["location"]
        addr = loc.get("address")
        if not addr and isinstance(loc.get("address_json"), str):
            try:
                addr_obj = json.loads(loc["address_json"])
                if isinstance(addr_obj, dict):
                    addr = addr_obj.get("street_address") or addr_obj.get("city_name")
            except json.JSONDecodeError:
                addr = None
        location = Location(
            location_id=str(loc.get("id") or loc.get("pk") or "") or None,
            name=loc.get("name") if isinstance(loc.get("name"), str) else None,
            address=addr if isinstance(addr, str) else None,
            lat=_safe_float(loc.get("lat")),
            lng=_safe_float(loc.get("lng")),
        )

    tagged_users_map: dict[str, UserRef] = {}
    for edge_node in _extract_edge_nodes(primary.get("edge_media_to_tagged_user")):
        u = _user_ref_from_dict(edge_node.get("user"))
        key = u.username or u.user_id
        if key and key not in tagged_users_map:
            tagged_users_map[key] = u
    usertags = primary.get("usertags")
    if isinstance(usertags, dict) and isinstance(usertags.get("in"), list):
        for entry in usertags["in"]:
            if isinstance(entry, dict):
                u = _user_ref_from_dict(entry.get("user"))
                key = u.username or u.user_id
                if key and key not in tagged_users_map:
                    tagged_users_map[key] = u

    like_count = _safe_int(primary.get("like_count"))
    if like_count is None:
        for like_key in ("edge_media_preview_like", "edge_liked_by"):
            if isinstance(primary.get(like_key), dict):
                like_count = _safe_int(primary[like_key].get("count"))
                if like_count is not None:
                    break

    is_collab = bool(primary.get("coauthor_producers"))

    candidates: list[IgMediaDownloadCandidate] = []
    sidecar_nodes = _extract_edge_nodes(primary.get("edge_sidecar_to_children"))
    carousel_nodes = (
        [c for c in primary["carousel_media"] if isinstance(c, dict)]
        if isinstance(primary.get("carousel_media"), list)
        else []
    )

    if sidecar_nodes or carousel_nodes:
        post_type: Literal["post", "carousel", "reel", "album_photo", "shared", "other"] = "carousel"
        children = sidecar_nodes or carousel_nodes
        for child in children:
            # Every child in sidecar/carousel_media is an actual slide (not a duplicate cover).
            candidates.append(_media_candidate_from_node(child, is_carousel_cover=False))
    else:
        cand = _media_candidate_from_node(primary, is_carousel_cover=False)
        candidates.append(cand)
        post_type = "reel" if is_reel else "post"

    all_comments: list[Comment] = []
    for node in matching_nodes:
        all_comments.extend(_collect_comments_from_media_node(node))
    paginated_top, replies_by_parent = _collect_paginated_comments(payloads, pk_id)
    all_comments.extend(paginated_top)
    merged_comments = _merge_comments(all_comments)
    if replies_by_parent:
        remaining_parent_ids = dict(replies_by_parent)
        for top_c in merged_comments:
            if top_c.comment_id and top_c.comment_id in remaining_parent_ids:
                extra_replies = remaining_parent_ids.pop(top_c.comment_id)
                top_c.replies = _merge_comments([*top_c.replies, *extra_replies])
        if remaining_parent_ids:
            orphan_replies = [r for grp in remaining_parent_ids.values() for r in grp]
            merged_comments = _merge_comments([*merged_comments, *orphan_replies])

    post = Post(
        platform="instagram",
        post_id=post_id,
        shortcode=shortcode,
        url=url,
        post_type=post_type,
        author=author,
        created_at=created_at,
        timestamp_unix=ts,
        caption=caption,
        hashtags=hashtags,
        location=location,
        tagged_users=list(tagged_users_map.values()),
        likes=LikesInfo(count=like_count),
        comments=merged_comments,
        is_collab=is_collab,
        source="playwright",
    )
    return post, candidates


_EXCLUDED_USER_LIST_CONTAINERS = frozenset(
    {
        "xdt_api__v1__feed__timeline__connection",
        "xdt_api__v1__discover__web__explore_grid__connection",
        "explore_story",
        "ad",
        "injected",
        "chaining_results",
        "suggested_users",
        "suggested_users_list",
        "suggestions",
        "ayml_users",
        "coauthor_producers",
        "get_slide_mailbox_for_iris_subscription",
        "threads_by_folder",
        "as_ig_direct_thread",
        "direct_threads",
        "inbox",
    }
)
_PLACEHOLDER_IG_USERNAMES = frozenset({"instagram 사용자", "instagram user"})
_DIALOG_SCROLL_JS = """() => {
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
  if (dialogs.length === 0) {
    return { hasDialog: false, x: 0, y: 0, height: 0, scrollTop: 0, moved: false, links: 0 };
  }
  for (let i = dialogs.length - 1; i >= 0; i--) {
    const dlg = dialogs[i];
    const els = [dlg, ...dlg.querySelectorAll('*')];
    const scrollables = [];
    for (const el of els) {
      if (el.clientHeight <= 0 || el.scrollHeight <= el.clientHeight + 5) continue;
      const oy = window.getComputedStyle(el).overflowY;
      if (oy === 'auto' || oy === 'scroll' || oy === 'overlay') {
        scrollables.push(el);
      }
    }
    if (scrollables.length > 0) {
      scrollables.sort((a, b) => {
        const aLinks = a.querySelectorAll('a[href]').length;
        const bLinks = b.querySelectorAll('a[href]').length;
        if (bLinks !== aLinks) return bLinks - aLinks;
        return b.scrollHeight - a.scrollHeight;
      });
      const target = scrollables[0];
      const r = target.getBoundingClientRect();
      const x = Math.max(10, Math.min(r.x + r.width / 2, window.innerWidth - 10));
      const y = Math.max(10, Math.min(r.y + r.height / 2, window.innerHeight - 10));
      const links = target.querySelectorAll('a[href]');
      return {
        hasDialog: true,
        x,
        y,
        height: target.scrollHeight,
        scrollTop: target.scrollTop,
        moved: false,
        links: links.length,
      };
    }
  }
  const topDlg = dialogs[dialogs.length - 1];
  const r = topDlg.getBoundingClientRect();
  const x = Math.max(10, Math.min(r.x + r.width / 2, window.innerWidth - 10));
  const y = Math.max(10, Math.min(r.y + r.height / 2, window.innerHeight - 10));
  const links = topDlg.querySelectorAll('a[href]');
  if (links.length > 0) {
    try { links[links.length - 1].scrollIntoView({ block: 'end' }); } catch (e) {}
  }
  return { hasDialog: true, x, y, height: topDlg.scrollHeight, scrollTop: 0, moved: false, links: links.length };
}"""
_COMMENT_PANE_SCROLL_JS = """() => {
  const roots = Array.from(document.querySelectorAll('article, main'));
  let best = null;
  for (const root of roots) {
    for (const el of root.querySelectorAll('ul, div')) {
      if (el.clientHeight < 150 || el.scrollHeight <= el.clientHeight + 40) continue;
      const oy = window.getComputedStyle(el).overflowY;
      if (oy !== 'auto' && oy !== 'scroll' && oy !== 'overlay') continue;
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  }
  if (!best) return 0;
  const h = best.scrollHeight;
  best.scrollTop = h;
  best.dispatchEvent(new Event('scroll', { bubbles: true }));
  return h;
}"""


def _walk_filtered_dicts(
    obj: Any,
    excluded_keys: frozenset[str],
) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for key, val in obj.items():
            if key in excluded_keys:
                continue
            yield from _walk_filtered_dicts(val, excluded_keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_filtered_dicts(item, excluded_keys)


def _is_direct_thread_dict(d: dict[str, Any]) -> bool:
    return any(k in d for k in ("thread_key", "thread_title", "slide_read_receipts", "thread_subtype"))


def extract_ig_likers_from_payloads(payloads: Sequence[dict[str, Any]]) -> list[UserRef]:
    """Extract deduplicated users who liked a post from modal /likers/ or GraphQL payloads."""
    seen: dict[str, UserRef] = {}
    for payload in payloads:
        for d in _walk_filtered_dicts(payload, _EXCLUDED_USER_LIST_CONTAINERS):
            user_dicts: list[dict[str, Any]] = []
            if isinstance(d.get("likers_connection"), dict):
                user_dicts.extend(_extract_edge_nodes(d["likers_connection"]))
            if isinstance(d.get("edge_liked_by"), dict):
                user_dicts.extend(_extract_edge_nodes(d["edge_liked_by"]))
            if isinstance(d.get("users"), list) and not _is_direct_thread_dict(d):
                user_dicts.extend(u for u in d["users"] if isinstance(u, dict))
            for raw_user in user_dicts:
                uname = raw_user.get("username")
                if not isinstance(uname, str) or not uname or uname.strip().lower() in _PLACEHOLDER_IG_USERNAMES:
                    continue
                ref = _user_ref_from_dict(raw_user)
                key = ref.user_id or ref.username
                if key and key not in seen:
                    seen[key] = ref
    return list(seen.values())


def extract_ig_connections_from_payloads(
    payloads: Sequence[dict[str, Any]],
    relation: Literal["follower", "following"],
    *,
    exclude_username: str | None = None,
) -> tuple[list[Connection], dict[str, str]]:
    """Extract followers or following Connection entries and avatar URL map."""
    exclude_norm = exclude_username.strip().lower() if exclude_username else None
    seen: dict[str, Connection] = {}
    avatar_urls: dict[str, str] = {}

    for payload in payloads:
        for d in _walk_filtered_dicts(payload, _EXCLUDED_USER_LIST_CONTAINERS):
            user_dicts: list[dict[str, Any]] = []
            if isinstance(d.get("users"), list) and not _is_direct_thread_dict(d):
                user_dicts.extend(u for u in d["users"] if isinstance(u, dict))
            edge_key = "edge_followed_by" if relation == "follower" else "edge_follow"
            if isinstance(d.get(edge_key), dict):
                user_dicts.extend(_extract_edge_nodes(d[edge_key]))

            for raw_user in user_dicts:
                username = raw_user.get("username")
                if (
                    not isinstance(username, str)
                    or not username
                    or username.strip().lower() in _PLACEHOLDER_IG_USERNAMES
                ):
                    continue
                if exclude_norm and username.strip().lower() == exclude_norm:
                    continue
                uid = str(raw_user.get("id") or raw_user.get("pk") or "") or None
                full_name = (
                    raw_user.get("full_name")
                    if isinstance(raw_user.get("full_name"), str)
                    else None
                )
                key = uid or username
                if key not in seen:
                    seen[key] = Connection(
                        platform="instagram",
                        relation=relation,
                        user_id=uid,
                        username=username,
                        full_name=full_name,
                        profile_url=f"https://www.instagram.com/{username}/",
                        match_confidence="exact",
                    )
                pic = raw_user.get("profile_pic_url_hd") or raw_user.get("profile_pic_url")
                if isinstance(pic, str) and pic and username not in avatar_urls:
                    avatar_urls[username] = pic

    return list(seen.values()), avatar_urls


def _is_highlight_item(
    item: dict[str, Any],
    *,
    from_edge_highlight_reels: bool = False,
    owner_username: str | None = None,
) -> bool:
    hid = str(item.get("id") or "")
    if not hid:
        return False
    if not from_edge_highlight_reels:
        reel_type = str(item.get("reel_type") or "").lower()
        if not (hid.startswith("highlight:") or reel_type == "highlight_reel"):
            return False
    if owner_username:
        user_obj = item.get("user") if isinstance(item.get("user"), dict) else item.get("owner")
        if isinstance(user_obj, dict) and isinstance(user_obj.get("username"), str) and user_obj["username"]:
            if user_obj["username"].strip().lower() != owner_username.strip().lower():
                return False
    return True


def _extract_highlight_cover_url(item: dict[str, Any]) -> str | None:
    cover_media = item.get("cover_media")
    if isinstance(cover_media, dict):
        for key in ("cropped_image_version", "full_image_version"):
            sub = cover_media.get(key)
            if isinstance(sub, dict) and isinstance(sub.get("url"), str) and sub["url"]:
                return sub["url"]
        if isinstance(cover_media.get("thumbnail_src"), str) and cover_media["thumbnail_src"]:
            return cover_media["thumbnail_src"]
    return None


def _collect_highlight_tray_entries(d: dict[str, Any]) -> list[tuple[dict[str, Any], bool]]:
    entries: list[tuple[dict[str, Any], bool]] = [
        (item, True) for item in _extract_edge_nodes(d.get("edge_highlight_reels"))
    ]
    if isinstance(d.get("tray"), list):
        entries.extend((t, False) for t in d["tray"] if isinstance(t, dict))
    for key, val in d.items():
        if key == "edge_highlight_reels":
            continue
        if isinstance(val, dict) and isinstance(val.get("edges"), list):
            for node in _extract_edge_nodes(val):
                nid = str(node.get("id") or "")
                if nid.startswith("highlight:") or str(node.get("reel_type") or "").lower() == "highlight_reel":
                    entries.append((node, False))
    return entries


def extract_ig_highlight_tray_ids(
    payloads: Sequence[dict[str, Any]],
    *,
    owner_username: str | None = None,
) -> list[str]:
    """Extract ordered unique highlight reel IDs from profile tray payloads."""
    seen: set[str] = set()
    ordered: list[str] = []
    for payload in payloads:
        for d in _walk_dicts(payload):
            for item, from_edge in _collect_highlight_tray_entries(d):
                if not _is_highlight_item(
                    item,
                    from_edge_highlight_reels=from_edge,
                    owner_username=owner_username,
                ):
                    continue
                hid = str(item.get("id") or "")
                if hid and hid not in seen:
                    seen.add(hid)
                    ordered.append(hid)
    return ordered


def extract_ig_highlights_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    owner_username: str | None = None,
) -> tuple[list[Story], dict[str, str]]:
    """Extract Instagram highlight stories and a map of `story_id -> media_url`."""
    highlight_meta: dict[str, tuple[str | None, str | None]] = {}
    stories: list[Story] = []
    seen_story_ids: set[str] = set()
    media_urls: dict[str, str] = {}

    # Pass 1: collect all highlight group metadata (title, cover) regardless of JSON key order.
    for payload in payloads:
        for d in _walk_dicts(payload):
            for item, from_edge in _collect_highlight_tray_entries(d):
                if not _is_highlight_item(
                    item,
                    from_edge_highlight_reels=from_edge,
                    owner_username=owner_username,
                ):
                    continue
                hid = str(item.get("id") or "")
                title = item.get("title") if isinstance(item.get("title"), str) and item["title"] else None
                cover_url = _extract_highlight_cover_url(item)
                prev_title, prev_cover = highlight_meta.get(hid, (None, None))
                highlight_meta[hid] = (title or prev_title, cover_url or prev_cover)

    # Pass 2: extract highlight story items and join with collected highlight metadata.
    for payload in payloads:
        for d in _walk_dicts(payload):
            reel_groups: list[dict[str, Any]] = []
            if isinstance(d.get("reels_media"), list):
                reel_groups.extend(g for g in d["reels_media"] if isinstance(g, dict))
            if isinstance(d.get("reels"), dict):
                reel_groups.extend(g for g in d["reels"].values() if isinstance(g, dict))
            if isinstance(d.get("items"), list) and str(d.get("id") or "").startswith("highlight:"):
                reel_groups.append(d)

            for group in reel_groups:
                hid = str(group.get("id") or "")
                if not hid:
                    continue
                if hid not in highlight_meta and f"highlight:{hid}" in highlight_meta:
                    hid = f"highlight:{hid}"
                is_known_meta = hid in highlight_meta
                if not _is_highlight_item(
                    group,
                    from_edge_highlight_reels=is_known_meta,
                    owner_username=owner_username,
                ):
                    continue
                title = group.get("title") if isinstance(group.get("title"), str) else None
                meta_title, meta_cover = highlight_meta.get(hid, (None, None))
                resolved_title = title or meta_title
                resolved_cover = _extract_highlight_cover_url(group) or meta_cover
                if resolved_cover:
                    media_urls.setdefault(f"cover:{hid}", resolved_cover)

                for raw_story in group.get("items", []):
                    if not isinstance(raw_story, dict):
                        continue
                    sid = str(raw_story.get("id") or raw_story.get("pk") or "")
                    if not sid or sid in seen_story_ids:
                        continue
                    seen_story_ids.add(sid)
                    ts = _safe_int(raw_story.get("taken_at_timestamp") or raw_story.get("taken_at"))
                    cand = _media_candidate_from_node(raw_story)
                    best_url = cand.progressive_url or cand.dash_video_url
                    if best_url:
                        media_urls[sid] = best_url
                    stories.append(
                        Story(
                            platform="instagram",
                            story_id=sid,
                            created_at=_ts_to_dt(ts),
                            timestamp_unix=ts,
                            source="highlight_web",
                            highlight_id=hid,
                            highlight_title=resolved_title,
                            media=MediaItem(
                                media_id=cand.media_id,
                                media_type=cand.media_type,
                                original_uri=best_url,
                                width=cand.width,
                                height=cand.height,
                            ),
                        )
                    )

    return stories, media_urls


def expand_ig_comments(
    page: Any,
    cooldown: CooldownTracker,
    max_clicks: int = 30,
    max_comment_scrolls: int = 8,
) -> int:
    """Click visible Instagram expand/reply buttons and scroll desktop comment panes."""
    clicks = 0
    scrolls = 0
    last_pane_height = -1
    stagnant_scrolls = 0

    while clicks < max_clicks:
        clicked_this_round = False
        for label in _EXPAND_BUTTON_LABELS:
            try:
                locator = page.get_by_role("button", name=label)
                count = locator.count()
            except Exception:
                continue

            for idx in range(count):
                if clicks >= max_clicks:
                    break
                btn = locator.nth(idx)
                try:
                    if not btn.is_visible():
                        continue
                    btn.click()
                except Exception:
                    continue
                clicks += 1
                clicked_this_round = True
                cooldown.wait_step()
                break
            if clicked_this_round:
                break

        if clicked_this_round:
            continue

        if scrolls >= max_comment_scrolls or stagnant_scrolls >= 1:
            break
        try:
            pane_height = page.evaluate(_COMMENT_PANE_SCROLL_JS)
        except Exception:
            break
        scrolls += 1
        if not isinstance(pane_height, (int, float)) or pane_height <= 0:
            break
        cooldown.wait_step()
        if pane_height == last_pane_height:
            stagnant_scrolls += 1
            break
        last_pane_height = int(pane_height)
    return clicks


def _write_json_atomic(path: Path, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, payload)


def download_post_media(
    request_context: Any,
    shortcode: str,
    candidates: Sequence[Any],
    media_dir: Path,
    *,
    created_timestamp: int | None = None,
    rel_dir: str = "media/instagram/posts",
) -> list[MediaItem]:
    """Download, verify, and (if needed) DASH-mux media items for an Instagram/Facebook post."""
    os.makedirs(to_long_path(media_dir), exist_ok=True)
    headers = {
        "Referer": "https://www.facebook.com/"
        if "facebook" in rel_dir
        else "https://www.instagram.com/"
    }
    saved_items: list[MediaItem] = []

    for idx, cand in enumerate(candidates, start=1):
        ext = ".mp4" if cand.media_type == "video" else ".jpg"
        filename = f"{shortcode}_{idx:02d}{ext}"
        dest = media_dir / filename
        rel_path = normalize_rel_path(f"{rel_dir}/{filename}")

        width = cand.width
        height = cand.height
        has_audio: bool | None = None

        if cand.media_type == "video":
            if cand.progressive_url:
                download_media_via_context(request_context, cand.progressive_url, dest, headers=headers)
                mp4_res = inspect_mp4(dest)
            else:
                mp4_res = None

            needs_mux = (
                (mp4_res is None or not mp4_res.ok or not mp4_res.has_audio)
                and bool(cand.dash_video_url)
                and bool(cand.dash_audio_url)
            )
            if needs_mux and cand.dash_video_url and cand.dash_audio_url:
                tmp_v_path: Path | None = None
                tmp_a_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=to_long_path(media_dir), suffix=".dash_v.mp4", delete=False
                    ) as tv:
                        tmp_v_path = Path(tv.name)
                    with tempfile.NamedTemporaryFile(
                        dir=to_long_path(media_dir), suffix=".dash_a.m4a", delete=False
                    ) as ta:
                        tmp_a_path = Path(ta.name)
                    download_media_via_context(
                        request_context, cand.dash_video_url, tmp_v_path, headers=headers
                    )
                    download_media_via_context(
                        request_context, cand.dash_audio_url, tmp_a_path, headers=headers
                    )
                    mux_dash_streams(tmp_v_path, tmp_a_path, dest)
                finally:
                    for tmp_p in (tmp_v_path, tmp_a_path):
                        if tmp_p is not None and tmp_p.exists():
                            try:
                                os.remove(to_long_path(tmp_p))
                            except OSError:
                                pass
                mp4_res = inspect_mp4(dest)

            if mp4_res is None or not mp4_res.ok:
                err = mp4_res.error if mp4_res else "no downloadable video URL"
                raise RuntimeError(f"Video verification failed for {shortcode}#{idx}: {err}")
            has_audio = mp4_res.has_audio
        else:
            if not cand.progressive_url:
                raise RuntimeError(f"Missing image URL for {shortcode}#{idx}")
            download_media_via_context(request_context, cand.progressive_url, dest, headers=headers)
            img_res = verify_image(dest)
            if not img_res.ok:
                raise RuntimeError(f"Image verification failed for {shortcode}#{idx}: {img_res.error}")
            width = img_res.width or width
            height = img_res.height or height

        if created_timestamp is not None:
            set_file_mtime(dest, created_timestamp)

        saved_items.append(
            MediaItem(
                media_id=cand.media_id,
                media_type=cand.media_type,
                local_path=rel_path,
                original_uri=cand.progressive_url or cand.dash_video_url,
                width=width,
                height=height,
                has_audio=has_audio,
                sha256=sha256_file(dest),
                byte_size=os.path.getsize(to_long_path(dest)),
                is_carousel_cover=cand.is_carousel_cover,
            )
        )

    return saved_items


class IgCollector:
    """Orchestrates Playwright CDP Instagram passes with atomic checkpoints and safety stops."""

    def __init__(
        self,
        page: Any,
        config: IgCollectConfig,
        request_context: Any | None = None,
    ) -> None:
        self.page = page
        self.config = config
        if request_context is not None:
            self.request_context = request_context
        else:
            ctx = getattr(page, "context", None)
            self.request_context = getattr(ctx, "request", None)
        self.collector = ResponseCollector(
            url_substrings=("/graphql/query", "/api/graphql", "/graphql", "/api/v1/")
        )
        self.collector.attach(self.page)

    def _safe_body_text(self) -> str:
        try:
            text_attr = getattr(self.page, "inner_text", None)
            if callable(text_attr):
                return text_attr("body") or ""
        except Exception:
            pass
        return ""

    def _harvest_dom_shortcodes(self) -> list[str]:
        try:
            hrefs = self.page.evaluate(
                "() => Array.from(document.querySelectorAll('main a[href]'), a => a.getAttribute('href') || '')"
            )
            if isinstance(hrefs, list):
                return extract_ig_dom_shortcodes(hrefs)
        except Exception:
            pass
        return []

    def _harvest_dom_highlights(self) -> list[dict[str, Any]]:
        try:
            raw_items = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href*="/stories/highlights/"]')).map(a => {
                  const img = a.querySelector('img');
                  return {
                    href: a.getAttribute('href') || '',
                    text: (a.innerText || '').trim(),
                    imgSrc: img ? (img.getAttribute('src') || '') : '',
                  };
                })"""
            )
            if not isinstance(raw_items, list):
                return []
        except Exception:
            return []

        tray: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            href = item.get("href")
            if not isinstance(href, str):
                continue
            m = _DOM_HIGHLIGHT_HREF_RE.search(href)
            if not m:
                continue
            hid = f"highlight:{m.group(1)}"
            if hid in seen:
                continue
            seen.add(hid)
            entry: dict[str, Any] = {
                "id": hid,
                "reel_type": "highlight_reel",
                "user": {"username": self.config.username},
            }
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                entry["title"] = text.strip()
            img_src = item.get("imgSrc")
            if isinstance(img_src, str) and img_src.strip():
                entry["cover_media"] = {"thumbnail_src": img_src.strip()}
            tray.append(entry)
        return tray

    def _visit_and_collect(self, url: str) -> list[dict[str, Any]]:
        self.collector.drain()
        self.page.goto(url, wait_until="domcontentloaded")
        self.config.cooldown.wait_step()
        self.collector.raise_if_unsafe()
        check_page_safety(str(getattr(self.page, "url", url)), self._safe_body_text())

        payloads: list[dict[str, Any]] = []
        try:
            html = self.page.content()
            if isinstance(html, str) and html:
                payloads.extend(extract_ssr_json_payloads(html))
        except Exception:
            pass
        for resp in self.collector.drain():
            payloads.extend(resp.payloads)
        return payloads

    def _scroll_and_collect(
        self,
        max_scrolls: int,
        stop_count: int | None = None,
        *,
        owner_username: str | None = None,
        dom_shortcodes: list[str] | None = None,
        scroll_target: Literal["window", "dialog"] = "window",
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        last_height = -1
        last_scroll_top = -1
        last_links = -1
        stagnant = 0
        max_stagnant = 4 if scroll_target == "dialog" else 2

        for _ in range(max_scrolls):
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", "")), self._safe_body_text())
            pre_drained = self.collector.drain()
            for resp in pre_drained:
                collected.extend(resp.payloads)
            if dom_shortcodes is not None:
                for sc in self._harvest_dom_shortcodes():
                    if sc not in dom_shortcodes:
                        dom_shortcodes.append(sc)

            if stop_count is not None:
                found = list(
                    dict.fromkeys(
                        [
                            *(dom_shortcodes or []),
                            *extract_ig_shortcodes_from_payloads(
                                collected, owner_username=owner_username
                            ),
                        ]
                    )
                )
                if len(found) >= stop_count:
                    break

            moved = False
            scroll_top = -1
            links_cnt = -1
            try:
                if scroll_target == "dialog":
                    res = self.page.evaluate(_DIALOG_SCROLL_JS)
                    if isinstance(res, dict):
                        if res.get("hasDialog") is False:
                            break
                        height = int(res.get("height") or 0)
                        scroll_top = int(res.get("scrollTop") or 0)
                        moved = bool(res.get("moved"))
                        links_cnt = int(res.get("links") or 0)
                        mouse = getattr(self.page, "mouse", None)
                        rx = res.get("x")
                        ry = res.get("y")
                        if mouse is not None and isinstance(rx, (int, float)) and isinstance(ry, (int, float)) and rx > 0 and ry > 0:
                            mouse.move(float(rx), float(ry))
                            mouse.wheel(0, 1500)
                    else:
                        height = int(res or 0)
                else:
                    height = self.page.evaluate("() => document.body.scrollHeight")
                    self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            self.config.cooldown.wait_step()

            post_drained = self.collector.drain()
            new_payloads_cnt = sum(len(r.payloads) for r in pre_drained) + sum(
                len(r.payloads) for r in post_drained
            )
            for resp in post_drained:
                collected.extend(resp.payloads)

            progressed = (
                height != last_height
                or moved
                or (scroll_top >= 0 and scroll_top != last_scroll_top)
                or (links_cnt >= 0 and links_cnt != last_links)
                or new_payloads_cnt > 0
            )
            if not progressed:
                stagnant += 1
                if stagnant >= max_stagnant:
                    break
            else:
                stagnant = 0
                last_height = height
                if scroll_top >= 0:
                    last_scroll_top = scroll_top
                if links_cnt >= 0:
                    last_links = links_cnt

        for resp in self.collector.drain():
            collected.extend(resp.payloads)
        if dom_shortcodes is not None:
            for sc in self._harvest_dom_shortcodes():
                if sc not in dom_shortcodes:
                    dom_shortcodes.append(sc)
        return collected

    def _visit_and_scroll_grid(
        self,
        url: str,
        *,
        stop_count: int | None = None,
        owner_username: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        payloads = self._visit_and_collect(url)
        dom_codes = self._harvest_dom_shortcodes()
        initial_codes = list(
            dict.fromkeys(
                [
                    *dom_codes,
                    *extract_ig_shortcodes_from_payloads(payloads, owner_username=owner_username),
                ]
            )
        )
        rem_stop: int | None = None
        if stop_count is not None:
            if len(initial_codes) >= stop_count:
                return payloads, initial_codes
            rem_stop = stop_count
        scroll_dom = list(initial_codes)
        payloads.extend(
            self._scroll_and_collect(
                self.config.max_scrolls,
                stop_count=rem_stop,
                owner_username=owner_username,
                dom_shortcodes=scroll_dom,
            )
        )
        combined = list(
            dict.fromkeys(
                [
                    *scroll_dom,
                    *extract_ig_shortcodes_from_payloads(payloads, owner_username=owner_username),
                ]
            )
        )
        return payloads, combined

    def run_pass_core(self) -> IgCollectSummary:
        """Pass 1: profile index, post details + comments + media/DASH mux, and highlights."""
        summary = IgCollectSummary(collect_pass="core")
        out = self.config.output_dir
        profile_payloads: list[dict[str, Any]] = []

        try:
            if self.config.shortcodes:
                shortcodes = list(dict.fromkeys(self.config.shortcodes))
            else:
                profile_url = f"https://www.instagram.com/{self.config.username}/"
                profile_payloads, shortcodes = self._visit_and_scroll_grid(
                    profile_url,
                    stop_count=self.config.limit,
                    owner_username=self.config.username,
                )
                dom_highlights = self._harvest_dom_highlights()
                if dom_highlights:
                    profile_payloads.append({"tray": dom_highlights})
                profile, avatar_url = extract_ig_profile_from_payloads(
                    profile_payloads,
                    target_username=self.config.username,
                )
                if profile is not None:
                    if avatar_url and self.config.download_avatars and self.request_context is not None:
                        avatar_dest = out / "media" / "profile" / f"{self.config.username}.jpg"
                        try:
                            download_media_via_context(
                                self.request_context,
                                avatar_url,
                                avatar_dest,
                                headers={"Referer": "https://www.instagram.com/"},
                            )
                            profile.avatar_local_path = normalize_rel_path(
                                f"media/instagram/profile/{self.config.username}.jpg"
                            )
                        except Exception:
                            pass
                    _write_json_atomic(out / "profile.json", profile.model_dump(mode="json"))
                if self.config.limit is None or len(shortcodes) < self.config.limit:
                    reels_url = f"https://www.instagram.com/{self.config.username}/reels/"
                    rem = (self.config.limit - len(shortcodes)) if self.config.limit is not None else None
                    try:
                        _, reel_codes = self._visit_and_scroll_grid(
                            reels_url,
                            stop_count=rem,
                            owner_username=self.config.username,
                        )
                        shortcodes = list(dict.fromkeys([*shortcodes, *reel_codes]))
                    except SafetyStopError:
                        raise
                    except Exception:
                        pass
                idx_path = out / "index_shortcodes.json"
                if idx_path.exists():
                    try:
                        existing_codes = json.loads(Path(to_long_path(idx_path)).read_text(encoding="utf-8"))
                        if isinstance(existing_codes, list):
                            shortcodes = list(
                                dict.fromkeys(
                                    [*shortcodes, *(c for c in existing_codes if isinstance(c, str))]
                                )
                            )
                    except Exception:
                        pass
                _write_json_atomic(idx_path, shortcodes)

            store = CheckpointStore(out / "state_core.json")
            store.ensure_items(shortcodes)
            summary.skipped_done = len(store.ids_by_status("done"))

            todo = store.pending_ids()
            if self.config.limit is not None:
                todo = todo[: self.config.limit]

            for sc in todo:
                summary.processed += 1
                try:
                    post_payloads = self._visit_and_collect(f"https://www.instagram.com/p/{sc}/")
                    expand_ig_comments(
                        self.page,
                        self.config.cooldown,
                        max_clicks=self.config.max_expand_clicks,
                    )
                    self.collector.raise_if_unsafe()
                    for resp in self.collector.drain():
                        post_payloads.extend(resp.payloads)

                    _write_json_atomic(out / "raw" / "posts" / f"{sc}.json", post_payloads)
                    store.set_status(sc, "fetched")

                    post, candidates = extract_ig_post_from_payloads(sc, post_payloads)
                    if post is None:
                        raise RuntimeError(f"Could not extract post payload for shortcode {sc}")
                    if not self.config.shortcodes and not _post_belongs_to_user(
                        sc, post, post_payloads, self.config.username
                    ):
                        raise RuntimeError(
                            f"Foreign post {sc} (author={post.author.username}) does not belong to {self.config.username}"
                        )

                    if self.request_context is not None and candidates:
                        saved_media = download_post_media(
                            self.request_context,
                            sc,
                            candidates,
                            out / "media" / "posts",
                            created_timestamp=post.timestamp_unix,
                        )
                        post.media = saved_media
                    store.set_status(sc, "media_saved")

                    _write_json_atomic(
                        out / "posts" / f"{sc}.json",
                        post.model_dump(mode="json"),
                    )
                    store.set_status(sc, "done")
                    summary.succeeded += 1
                    self.config.cooldown.record_item_completed()
                except SafetyStopError as exc:
                    store.set_status(sc, "failed", error=str(exc))
                    summary.failed += 1
                    summary.paused_reason = str(exc)
                    return summary
                except Exception as exc:
                    store.set_status(sc, "failed", error=str(exc))
                    summary.failed += 1

            if profile_payloads:
                tray_ids = extract_ig_highlight_tray_ids(
                    profile_payloads,
                    owner_username=self.config.username,
                )
                stories, media_urls = extract_ig_highlights_from_payloads(
                    profile_payloads,
                    owner_username=self.config.username,
                )
                covered_hids = {s.highlight_id for s in stories if s.highlight_id}
                extra_hl_payloads: list[dict[str, Any]] = []
                for hid in tray_ids:
                    if hid in covered_hids:
                        continue
                    num_id = hid.split(":", 1)[1] if ":" in hid else hid
                    try:
                        extra_hl_payloads.extend(
                            self._visit_and_collect(
                                f"https://www.instagram.com/stories/highlights/{num_id}/"
                            )
                        )
                    except SafetyStopError:
                        raise
                    except Exception:
                        continue
                if extra_hl_payloads:
                    stories, media_urls = extract_ig_highlights_from_payloads(
                        [*profile_payloads, *extra_hl_payloads],
                        owner_username=self.config.username,
                    )
                if stories:
                    cover_local_by_hid: dict[str, str] = {}
                    if self.request_context is not None:
                        for hid in dict.fromkeys(s.highlight_id for s in stories if s.highlight_id):
                            cov_url = media_urls.get(f"cover:{hid}")
                            if not cov_url:
                                continue
                            safe_hid = re.sub(r"[^A-Za-z0-9_-]", "_", hid)
                            cov_dest = out / "media" / "highlights" / f"cover_{safe_hid}.jpg"
                            try:
                                download_media_via_context(
                                    self.request_context,
                                    cov_url,
                                    cov_dest,
                                    headers={"Referer": "https://www.instagram.com/"},
                                )
                                cover_local_by_hid[hid] = normalize_rel_path(
                                    f"media/instagram/highlights/cover_{safe_hid}.jpg"
                                )
                            except Exception:
                                pass

                    for story in stories:
                        if story.highlight_id and story.highlight_id in cover_local_by_hid:
                            story.highlight_cover = cover_local_by_hid[story.highlight_id]
                        url = media_urls.get(story.story_id)
                        if url and story.media and self.request_context is not None:
                            ext = ".mp4" if story.media.media_type == "video" else ".jpg"
                            dest = out / "media" / "highlights" / f"{story.story_id}{ext}"
                            try:
                                download_media_via_context(
                                    self.request_context,
                                    url,
                                    dest,
                                    headers={"Referer": "https://www.instagram.com/"},
                                )
                                story.media.local_path = normalize_rel_path(
                                    f"media/instagram/highlights/{story.story_id}{ext}"
                                )
                            except Exception:
                                pass
                    _write_json_atomic(
                        out / "highlights.json",
                        [s.model_dump(mode="json") for s in stories],
                    )
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)

        return summary

    def run_pass_likes(self) -> IgCollectSummary:
        """Pass 2 (optional): collect liking users modal for collected shortcodes."""
        summary = IgCollectSummary(collect_pass="likes")
        out = self.config.output_dir
        if self.config.shortcodes:
            shortcodes = list(self.config.shortcodes)
        else:
            idx_path = out / "index_shortcodes.json"
            shortcodes = (
                json.loads(Path(to_long_path(idx_path)).read_text(encoding="utf-8"))
                if idx_path.exists()
                else []
            )

        store = CheckpointStore(out / "state_likes.json")
        store.ensure_items(shortcodes)
        summary.skipped_done = len(store.ids_by_status("done"))
        todo = store.pending_ids()
        if self.config.limit is not None:
            todo = todo[: self.config.limit]

        for sc in todo:
            summary.processed += 1
            try:
                payloads = self._visit_and_collect(f"https://www.instagram.com/p/{sc}/liked_by/")
                initial_users = extract_ig_likers_from_payloads(payloads)
                has_more_likers = any(
                    isinstance(d.get("likers_connection"), dict)
                    and isinstance(d["likers_connection"].get("page_info"), dict)
                    and bool(d["likers_connection"]["page_info"].get("has_next_page"))
                    for p in payloads
                    for d in _walk_dicts(p)
                )
                if not initial_users or has_more_likers:
                    payloads.extend(
                        self._scroll_and_collect(min(20, self.config.max_scrolls), scroll_target="window")
                    )
                users = extract_ig_likers_from_payloads(payloads)

                expected_count: int | None = None
                post_json = out / "posts" / f"{sc}.json"
                if post_json.exists():
                    existing_post = Post.model_validate_json(
                        Path(to_long_path(post_json)).read_text(encoding="utf-8")
                    )
                    expected_count = existing_post.likes.count

                likes_info = LikesInfo(
                    count=expected_count if expected_count is not None else len(users),
                    is_truncated=(expected_count is not None and len(users) < expected_count),
                    users=users,
                )
                _write_json_atomic(out / "raw" / "likes" / f"{sc}.json", payloads)
                _write_json_atomic(
                    out / "likes" / f"{sc}.json",
                    likes_info.model_dump(mode="json"),
                )
                store.set_status(sc, "done")
                summary.succeeded += 1
                self.config.cooldown.record_item_completed()
            except SafetyStopError as exc:
                store.set_status(sc, "failed", error=str(exc))
                summary.failed += 1
                summary.paused_reason = str(exc)
                return summary
            except Exception as exc:
                store.set_status(sc, "failed", error=str(exc))
                summary.failed += 1
        return summary

    def _has_open_dialog(self) -> bool | None:
        try:
            res = self.page.evaluate(
                "() => document.querySelectorAll('[role=\"dialog\"]').length > 0"
            )
            if isinstance(res, bool):
                return res
        except Exception:
            pass
        return None

    def _click_connections_trigger(self, suffix: Literal["followers", "following"]) -> bool:
        selectors = (
            f'a[href*="/{self.config.username}/{suffix}/"]',
            f'a[href*="/{suffix}/"]',
            f'header a[href*="{suffix}"]',
        )
        loc_fn = getattr(self.page, "locator", None)
        if callable(loc_fn):
            for sel in selectors:
                try:
                    loc = loc_fn(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.click()
                        return True
                except Exception:
                    continue

        labels = (
            ("팔로워", "followers")
            if suffix == "followers"
            else ("팔로우", "팔로잉", "following")
        )
        for label in labels:
            for role in ("link", "button"):
                try:
                    loc = self.page.get_by_role(role, name=label)
                    if loc.count() > 0 and loc.nth(0).is_visible():
                        loc.nth(0).click()
                        return True
                except Exception:
                    continue
        return False

    def run_pass_connections(self) -> IgCollectSummary:
        """Pass 2 (optional): collect followers and following lists with optional avatars."""
        summary = IgCollectSummary(collect_pass="connections")
        out = self.config.output_dir
        all_connections: list[Connection] = []

        for relation, suffix in (("follower", "followers"), ("following", "following")):
            summary.processed += 1
            try:
                url = f"https://www.instagram.com/{self.config.username}/{suffix}/"
                payloads = self._visit_and_collect(url)
                if self._has_open_dialog() is False:
                    clicked = self._click_connections_trigger(suffix)  # type: ignore[arg-type]
                    if not clicked:
                        profile_url = f"https://www.instagram.com/{self.config.username}/"
                        payloads.extend(self._visit_and_collect(profile_url))
                        clicked = self._click_connections_trigger(suffix)  # type: ignore[arg-type]
                    if clicked:
                        self.config.cooldown.wait_step()
                        self.collector.raise_if_unsafe()
                        for resp in self.collector.drain():
                            payloads.extend(resp.payloads)
                payloads.extend(self._scroll_and_collect(self.config.max_scrolls, scroll_target="dialog"))
                _write_json_atomic(out / "raw" / "connections" / f"{suffix}.json", payloads)
                conns, avatar_urls = extract_ig_connections_from_payloads(
                    payloads,
                    relation,  # type: ignore[arg-type]
                    exclude_username=self.config.username,
                )
                if self.config.download_avatars and self.request_context is not None:
                    for conn in conns:
                        if conn.username and conn.username in avatar_urls:
                            dest = out / "media" / "avatars" / f"{conn.username}.jpg"
                            try:
                                download_media_via_context(
                                    self.request_context,
                                    avatar_urls[conn.username],
                                    dest,
                                    headers={"Referer": "https://www.instagram.com/"},
                                )
                                conn.avatar_local_path = normalize_rel_path(
                                    f"media/instagram/avatars/{conn.username}.jpg"
                                )
                            except Exception:
                                pass
                all_connections.extend(conns)
                summary.succeeded += 1
            except SafetyStopError as exc:
                summary.failed += 1
                summary.paused_reason = str(exc)
                break
            except Exception:
                summary.failed += 1

        _write_json_atomic(
            out / "connections.json",
            [c.model_dump(mode="json") for c in all_connections],
        )
        return summary

    def _run_index_pass(self, collect_pass: Literal["tagged", "saved"], url_suffix: str) -> IgCollectSummary:
        summary = IgCollectSummary(collect_pass=collect_pass)
        out = self.config.output_dir
        summary.processed = 1
        try:
            url = f"https://www.instagram.com/{self.config.username}/{url_suffix}/"
            _, shortcodes = self._visit_and_scroll_grid(url, stop_count=self.config.limit)
            if self.config.limit is not None:
                shortcodes = shortcodes[: self.config.limit]
            _write_json_atomic(out / f"{collect_pass}_index.json", shortcodes)
            summary.succeeded = 1
        except SafetyStopError as exc:
            summary.failed = 1
            summary.paused_reason = str(exc)
        return summary

    def run_pass_tagged(self) -> IgCollectSummary:
        """Pass 3 (optional): collect tagged posts index, full post details, comments, and media."""
        summary = IgCollectSummary(collect_pass="tagged")
        out = self.config.output_dir
        try:
            if self.config.shortcodes:
                shortcodes = list(dict.fromkeys(self.config.shortcodes))
            else:
                url = f"https://www.instagram.com/{self.config.username}/tagged/"
                _, shortcodes = self._visit_and_scroll_grid(url, stop_count=self.config.limit)
                if self.config.limit is not None:
                    shortcodes = shortcodes[: self.config.limit]
                _write_json_atomic(out / "tagged_index.json", shortcodes)

            store = CheckpointStore(out / "state_tagged.json")
            store.ensure_items(shortcodes)
            summary.skipped_done = len(store.ids_by_status("done"))

            todo = store.pending_ids()
            if self.config.limit is not None:
                todo = todo[: self.config.limit]

            for sc in todo:
                summary.processed += 1
                try:
                    post_payloads = self._visit_and_collect(f"https://www.instagram.com/p/{sc}/")
                    self.collector.raise_if_unsafe()
                    for resp in self.collector.drain():
                        post_payloads.extend(resp.payloads)

                    _write_json_atomic(out / "raw" / "tagged_posts" / f"{sc}.json", post_payloads)
                    store.set_status(sc, "fetched")

                    post, candidates = extract_ig_post_from_payloads(sc, post_payloads)
                    if post is None:
                        raise RuntimeError(f"Could not extract tagged post payload for shortcode {sc}")
                    post.post_type = "tagged"
                    if (post.author.username or "").lower() != self.config.username.lower():
                        post.comments = []

                    if self.request_context is not None and candidates:
                        saved_media = download_post_media(
                            self.request_context,
                            sc,
                            candidates,
                            out / "media" / "tagged",
                            created_timestamp=post.timestamp_unix,
                            rel_dir="media/instagram/tagged",
                        )
                        post.media = saved_media
                    store.set_status(sc, "media_saved")

                    _write_json_atomic(
                        out / "tagged_posts" / f"{sc}.json",
                        post.model_dump(mode="json"),
                    )
                    store.set_status(sc, "done")
                    summary.succeeded += 1
                    self.config.cooldown.record_item_completed()
                except SafetyStopError as exc:
                    store.set_status(sc, "failed", error=str(exc))
                    summary.failed += 1
                    summary.paused_reason = str(exc)
                    return summary
                except Exception as exc:
                    store.set_status(sc, "failed", error=str(exc))
                    summary.failed += 1
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)

        return summary

    def run_pass_saved(self) -> IgCollectSummary:
        """Pass 3 (optional): collect saved posts index."""
        return self._run_index_pass("saved", "saved")


def collect_instagram(config: IgCollectConfig) -> IgCollectSummary:
    """Attach to running Chrome over CDP and run the selected Instagram collection pass."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        _browser, context = connect_cdp(pw, config.cdp_url)
        page = context.new_page()
        try:
            runner = IgCollector(page, config, request_context=context.request)
            dispatch = {
                "core": runner.run_pass_core,
                "likes": runner.run_pass_likes,
                "connections": runner.run_pass_connections,
                "tagged": runner.run_pass_tagged,
                "saved": runner.run_pass_saved,
            }
            return dispatch[config.collect_pass]()
        finally:
            page.close()
