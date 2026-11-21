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
_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")
_EXPAND_BUTTON_LABELS = (
    "숨겨진 댓글 보기",
    "댓글 더 보기",
    "답글 보기",
    "View hidden comments",
    "View more comments",
    "Load more comments",
    "View replies",
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
) -> tuple[Profile | None, str | None]:
    """Extract the richest Instagram Profile and avatar URL from SSR/API payloads."""
    best_score = -1
    best_profile: Profile | None = None
    best_avatar: str | None = None

    for payload in payloads:
        for node in _walk_dicts(payload):
            username = node.get("username")
            if not isinstance(username, str) or not username:
                continue

            has_gql = any(
                k in node
                for k in ("edge_owner_to_timeline_media", "edge_followed_by", "edge_follow")
            )
            has_v1 = any(
                k in node for k in ("media_count", "follower_count", "following_count")
            )
            if not (has_gql or has_v1):
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

            score = sum(
                x is not None
                for x in (user_id, full_name, bio, avatar_url, posts_cnt, followers_cnt, following_cnt)
            )
            if score > best_score:
                best_score = score
                best_avatar = avatar_url if isinstance(avatar_url, str) else None
                best_profile = Profile(
                    platform="instagram",
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                    bio=bio,
                    counts=ProfileCounts(
                        posts=posts_cnt,
                        followers=followers_cnt,
                        following=following_cnt,
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


def extract_ig_shortcodes_from_payloads(payloads: Sequence[dict[str, Any]]) -> list[str]:
    """Extract unique ordered post/reel shortcodes from timeline, tagged, or saved payloads."""
    seen: set[str] = set()
    ordered: list[str] = []

    for payload in payloads:
        for d in _walk_dicts(payload):
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

            if code not in seen:
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


def _collect_paginated_comments(
    payloads: Sequence[dict[str, Any]],
    target_media_id: str | None,
) -> list[Comment]:
    parsed: list[Comment] = []
    for payload in payloads:
        for d in _walk_dicts(payload):
            # v1 comments endpoint: {"comments": [...], ...}
            if isinstance(d.get("comments"), list) and "shortcode" not in d and "code" not in d:
                for c in d["comments"]:
                    if isinstance(c, dict) and ("text" in c or "pk" in c):
                        media_id_attr = str(c.get("media_id") or "")
                        if target_media_id and media_id_attr and media_id_attr != target_media_id:
                            continue
                        parsed.append(_parse_ig_comment_node(c))

            # GraphQL comments connection
            for key, val in d.items():
                if "comments__connection" in key or key == "edge_media_to_parent_comment":
                    for c in _extract_edge_nodes(val):
                        parsed.append(_parse_ig_comment_node(c))
    return parsed


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
        for idx, child in enumerate(children):
            candidates.append(_media_candidate_from_node(child, is_carousel_cover=(idx == 0)))
    else:
        cand = _media_candidate_from_node(primary, is_carousel_cover=False)
        candidates.append(cand)
        post_type = "reel" if is_reel else "post"

    all_comments: list[Comment] = []
    for node in matching_nodes:
        all_comments.extend(_collect_comments_from_media_node(node))
    all_comments.extend(_collect_paginated_comments(payloads, pk_id))
    merged_comments = _merge_comments(all_comments)

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


def extract_ig_likers_from_payloads(payloads: Sequence[dict[str, Any]]) -> list[UserRef]:
    """Extract deduplicated users who liked a post from modal /likers/ or GraphQL payloads."""
    seen: dict[str, UserRef] = {}
    for payload in payloads:
        for d in _walk_dicts(payload):
            user_dicts: list[dict[str, Any]] = []
            if isinstance(d.get("users"), list):
                user_dicts.extend(u for u in d["users"] if isinstance(u, dict))
            if isinstance(d.get("edge_liked_by"), dict):
                user_dicts.extend(_extract_edge_nodes(d["edge_liked_by"]))
            for raw_user in user_dicts:
                if "username" not in raw_user:
                    continue
                ref = _user_ref_from_dict(raw_user)
                key = ref.user_id or ref.username
                if key and key not in seen:
                    seen[key] = ref
    return list(seen.values())


def extract_ig_connections_from_payloads(
    payloads: Sequence[dict[str, Any]],
    relation: Literal["follower", "following"],
) -> tuple[list[Connection], dict[str, str]]:
    """Extract followers or following Connection entries and avatar URL map."""
    seen: dict[str, Connection] = {}
    avatar_urls: dict[str, str] = {}

    for payload in payloads:
        for d in _walk_dicts(payload):
            user_dicts: list[dict[str, Any]] = []
            if isinstance(d.get("users"), list):
                user_dicts.extend(u for u in d["users"] if isinstance(u, dict))
            edge_key = "edge_followed_by" if relation == "follower" else "edge_follow"
            if isinstance(d.get(edge_key), dict):
                user_dicts.extend(_extract_edge_nodes(d[edge_key]))

            for raw_user in user_dicts:
                username = raw_user.get("username")
                if not isinstance(username, str) or not username:
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


def extract_ig_highlights_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> tuple[list[Story], dict[str, str]]:
    """Extract Instagram highlight stories and a map of `story_id -> media_url`."""
    highlight_meta: dict[str, tuple[str | None, str | None]] = {}
    stories: list[Story] = []
    seen_story_ids: set[str] = set()
    media_urls: dict[str, str] = {}

    # Pass 1: collect all highlight group metadata (title, cover) regardless of JSON key order.
    for payload in payloads:
        for d in _walk_dicts(payload):
            tray_nodes: list[dict[str, Any]] = []
            tray_nodes.extend(_extract_edge_nodes(d.get("edge_highlight_reels")))
            if isinstance(d.get("tray"), list):
                tray_nodes.extend(t for t in d["tray"] if isinstance(t, dict))

            for item in tray_nodes:
                hid = str(item.get("id") or "")
                if not hid:
                    continue
                title = item.get("title") if isinstance(item.get("title"), str) else None
                cover_url: str | None = None
                cover_media = item.get("cover_media")
                if isinstance(cover_media, dict):
                    cropped = cover_media.get("cropped_image_version")
                    if isinstance(cropped, dict) and isinstance(cropped.get("url"), str):
                        cover_url = cropped["url"]
                    elif isinstance(cover_media.get("thumbnail_src"), str):
                        cover_url = cover_media["thumbnail_src"]
                highlight_meta[hid] = (title, cover_url)

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
                title = group.get("title") if isinstance(group.get("title"), str) else None
                meta_title, meta_cover = highlight_meta.get(hid, (None, None))
                resolved_title = title or meta_title
                if meta_cover:
                    media_urls.setdefault(f"cover:{hid}", meta_cover)

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
) -> int:
    """Click visible Instagram expand/reply buttons with human-like delays."""
    clicks = 0
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

        if not clicked_this_round:
            break
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
        self.collector = ResponseCollector(url_substrings=("/graphql/query", "/api/v1/"))
        self.collector.attach(self.page)

    def _safe_body_text(self) -> str:
        try:
            text_attr = getattr(self.page, "inner_text", None)
            if callable(text_attr):
                return text_attr("body") or ""
        except Exception:
            pass
        return ""

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

    def _scroll_and_collect(self, max_scrolls: int, stop_count: int | None = None) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        last_height = -1
        stagnant = 0

        for _ in range(max_scrolls):
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", "")), self._safe_body_text())
            for resp in self.collector.drain():
                collected.extend(resp.payloads)

            if stop_count is not None and len(extract_ig_shortcodes_from_payloads(collected)) >= stop_count:
                break

            try:
                height = self.page.evaluate("() => document.body.scrollHeight")
                self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            self.config.cooldown.wait_step()

            if height == last_height:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
                last_height = height

        for resp in self.collector.drain():
            collected.extend(resp.payloads)
        return collected

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
                profile_payloads = self._visit_and_collect(profile_url)
                profile_payloads.extend(
                    self._scroll_and_collect(
                        self.config.max_scrolls,
                        stop_count=self.config.limit,
                    )
                )
                profile, avatar_url = extract_ig_profile_from_payloads(profile_payloads)
                if profile is not None:
                    if avatar_url and self.config.download_avatars and self.request_context is not None:
                        avatar_dest = out / "media" / "profile" / f"{self.config.username}.jpg"
                        download_media_via_context(
                            self.request_context,
                            avatar_url,
                            avatar_dest,
                            headers={"Referer": "https://www.instagram.com/"},
                        )
                        profile.avatar_local_path = normalize_rel_path(
                            f"media/instagram/profile/{self.config.username}.jpg"
                        )
                    _write_json_atomic(out / "profile.json", profile.model_dump(mode="json"))
                shortcodes = extract_ig_shortcodes_from_payloads(profile_payloads)
                _write_json_atomic(out / "index_shortcodes.json", shortcodes)

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
                stories, media_urls = extract_ig_highlights_from_payloads(profile_payloads)
                if stories:
                    for story in stories:
                        url = media_urls.get(story.story_id)
                        if url and story.media and self.request_context is not None:
                            ext = ".mp4" if story.media.media_type == "video" else ".jpg"
                            dest = out / "media" / "highlights" / f"{story.story_id}{ext}"
                            download_media_via_context(
                                self.request_context,
                                url,
                                dest,
                                headers={"Referer": "https://www.instagram.com/"},
                            )
                            story.media.local_path = normalize_rel_path(
                                f"media/instagram/highlights/{story.story_id}{ext}"
                            )
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
                payloads.extend(self._scroll_and_collect(min(20, self.config.max_scrolls)))
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
                payloads.extend(self._scroll_and_collect(self.config.max_scrolls))
                conns, avatar_urls = extract_ig_connections_from_payloads(
                    payloads,
                    relation,  # type: ignore[arg-type]
                )
                if self.config.download_avatars and self.request_context is not None:
                    for conn in conns:
                        if conn.username and conn.username in avatar_urls:
                            dest = out / "media" / "avatars" / f"{conn.username}.jpg"
                            download_media_via_context(
                                self.request_context,
                                avatar_urls[conn.username],
                                dest,
                                headers={"Referer": "https://www.instagram.com/"},
                            )
                            conn.avatar_local_path = normalize_rel_path(
                                f"media/instagram/avatars/{conn.username}.jpg"
                            )
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
            payloads = self._visit_and_collect(url)
            payloads.extend(self._scroll_and_collect(self.config.max_scrolls, stop_count=self.config.limit))
            shortcodes = extract_ig_shortcodes_from_payloads(payloads)
            if self.config.limit is not None:
                shortcodes = shortcodes[: self.config.limit]
            _write_json_atomic(out / f"{collect_pass}_index.json", shortcodes)
            summary.succeeded = 1
        except SafetyStopError as exc:
            summary.failed = 1
            summary.paused_reason = str(exc)
        return summary

    def run_pass_tagged(self) -> IgCollectSummary:
        """Pass 3 (optional): collect tagged posts index."""
        return self._run_index_pass("tagged", "tagged")

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
