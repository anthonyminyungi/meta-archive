"""Facebook CDP collector and pure Comet GraphQL/SSR payload parsers."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import urllib.parse
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
from meta_archive_pipeline.collect.ig_collector import download_post_media, parse_dash_manifest
from meta_archive_pipeline.official.fb_parser import clean_fb_external_url, extract_domain
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    MediaItem,
    Post,
    Profile,
    ReactionSummary,
    SharedPostRef,
    UserRef,
)
from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.checkpoint import CheckpointStore
from meta_archive_pipeline.utils.fs import atomic_write_bytes
from meta_archive_pipeline.utils.media import download_media_via_context, verify_image

FbPass = Literal["posts", "comments", "friends", "albums"]

_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")
_RELEVANT_SORT_LABELS = (
    "가장 관련성 높은 댓글",
    "관련성 높은 댓글",
    "Most relevant",
    "Top comments",
)
_ALL_COMMENTS_LABELS = ("모든 댓글", "All comments")
_FB_EXPAND_LABELS = (
    "댓글 더 보기",
    "이전 댓글 더 보기",
    "이전 댓글 보기",
    "답글 보기",
    "답글 더 보기",
    "답글 모두 보기",
    "View more comments",
    "View previous comments",
    "View more replies",
    "View all replies",
)
_FB_EXPAND_REGEX = re.compile(
    r"(?:답글\s*\d*개?\s*(?:모두\s*)?보기|댓글\s*\d*개?\s*(?:더|이전)\s*보기|"
    r"View\s+(?:all\s+)?\d*\s*(?:more\s+|previous\s+)?(?:comments|replies))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FbMediaDownloadCandidate:
    """Download URLs and metadata for a Facebook post photo or video."""

    post_id: str
    media_id: str | None
    media_type: Literal["image", "video", "audio", "unknown"]
    progressive_url: str | None = None
    dash_video_url: str | None = None
    dash_audio_url: str | None = None
    width: int | None = None
    height: int | None = None
    is_carousel_cover: bool = False
    is_link_thumbnail: bool = False


@dataclass
class FbCollectConfig:
    """Configuration for a Facebook CDP collection pass."""

    output_dir: Path
    cdp_url: str = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}"
    collect_pass: FbPass = "posts"
    limit: int | None = None
    download_avatars: bool = True
    cooldown: CooldownTracker = field(default_factory=CooldownTracker)
    max_scrolls: int = 800
    max_stagnant_scrolls: int = 12
    max_expand_clicks: int = 20


@dataclass
class FbCollectSummary:
    """Result metrics for a Facebook collection pass."""

    collect_pass: FbPass
    items_saved: int = 0
    media_saved: int = 0
    comments_posts_fetched: int = 0
    paused_reason: str | None = None


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


def _ts_to_dt(ts: int | None) -> datetime | None:
    if ts is None or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _write_json_atomic(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _safe_filename_stem(raw: str, max_len: int = 96) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "item"
    if len(stem) <= max_len:
        return stem
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{stem[:64].rstrip('_')}_{digest}"


def _is_fb_owner_post(post: Post, owner: Profile | None) -> bool:
    """Return True when the post is authored by the archive owner (not a tagged/wall post by others)."""
    if owner is None:
        return True
    if owner.user_id and post.author.user_id:
        return post.author.user_id == owner.user_id
    if owner.full_name and post.author.full_name:
        return post.author.full_name.strip() == owner.full_name.strip()
    if owner.username and post.url:
        if f"/{owner.username.lower()}/" in post.url.lower():
            return True
    if post.post_type == "album_photo":
        return True
    return False


_EXCLUDED_FB_FRIEND_CONTAINERS = frozenset(
    {
        "people_you_may_know",
        "pymk",
        "friend_requests",
        "incoming_friend_requests",
        "outgoing_friend_requests",
        "suggested_friends",
        "followers",
        "following",
        "liked_pages",
        "groups",
    }
)
_NON_USER_FB_TYPENAMES = frozenset(
    {"Page", "Group", "Photo", "Video", "Event", "Application", "Place", "Story", "Media"}
)
_NON_USER_FB_URL_SEGMENTS = ("/groups/", "/pages/", "/events/", "/photo", "/videos/", "/watch/")
_EXCLUDED_FB_STORY_CONTAINERS = frozenset(
    {
        "attached_story",
        "sponsored_data",
        "comet_feed_unit_sponsored_data",
        "people_you_may_know",
        "pymk",
    }
)


def _walk_filtered_fb_dicts(
    obj: Any,
    excluded_keys: frozenset[str],
) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for key, val in obj.items():
            if key in excluded_keys:
                continue
            yield from _walk_filtered_fb_dicts(val, excluded_keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_filtered_fb_dicts(item, excluded_keys)


def extract_fb_friends_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> tuple[list[Connection], dict[str, str]]:
    """Extract deduplicated Facebook friends and an avatar URL map from Relay payloads."""
    seen: dict[str, Connection] = {}
    avatar_urls: dict[str, str] = {}

    def _consume_friend_node(node: dict[str, Any]) -> None:
        inner = node.get("node") if isinstance(node.get("node"), dict) else node
        if not isinstance(inner, dict):
            return

        nested = inner.get("node") if isinstance(inner.get("node"), dict) else None
        for candidate in (nested, inner):
            if isinstance(candidate, dict):
                tname = candidate.get("__typename")
                if isinstance(tname, str) and tname in _NON_USER_FB_TYPENAMES:
                    return

        uid = str(inner.get("id") or "") or None
        full_name: str | None = None
        if isinstance(inner.get("name"), str) and inner["name"]:
            full_name = inner["name"]
        elif isinstance(inner.get("title"), dict) and isinstance(inner["title"].get("text"), str):
            full_name = inner["title"]["text"]

        profile_url = inner.get("url") if isinstance(inner.get("url"), str) else None
        if profile_url and any(seg in profile_url.lower() for seg in _NON_USER_FB_URL_SEGMENTS):
            return
        if not full_name:
            return

        avatar_url: str | None = None
        for pic_key in ("image", "profile_picture", "profilePicture"):
            pic_obj = inner.get(pic_key)
            if isinstance(pic_obj, dict) and isinstance(pic_obj.get("uri"), str):
                avatar_url = pic_obj["uri"]
                break

        key = uid or profile_url or full_name
        if key not in seen:
            seen[key] = Connection(
                platform="facebook",
                relation="friend",
                user_id=uid,
                full_name=full_name,
                profile_url=profile_url,
                match_confidence="exact",
            )
        if avatar_url and key not in avatar_urls:
            avatar_urls[key] = avatar_url

    for payload in payloads:
        for d in _walk_filtered_fb_dicts(payload, _EXCLUDED_FB_FRIEND_CONTAINERS):
            for container_key in ("all_friends", "pageItems", "friends_list"):
                container = d.get(container_key)
                if isinstance(container, dict) and isinstance(container.get("edges"), list):
                    for edge in container["edges"]:
                        if isinstance(edge, dict):
                            _consume_friend_node(edge)

    return list(seen.values()), avatar_urls


def _extract_fb_reactions(feedback: Any) -> ReactionSummary:
    if not isinstance(feedback, dict):
        return ReactionSummary()

    total = 0
    rc = feedback.get("reaction_count")
    if isinstance(rc, dict):
        total = _safe_int(rc.get("count")) or 0
    elif isinstance(feedback.get("i18n_reaction_count"), str):
        digits = re.sub(r"\D+", "", feedback["i18n_reaction_count"])
        total = int(digits) if digits else 0

    by_type: dict[str, int] = {}
    top_reactions = feedback.get("top_reactions")
    if isinstance(top_reactions, dict) and isinstance(top_reactions.get("edges"), list):
        for edge in top_reactions["edges"]:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node") if isinstance(edge.get("node"), dict) else {}
            rtype = (
                node.get("reaction_type")
                or node.get("localized_name")
                or node.get("id")
                or "UNKNOWN"
            )
            cnt = _safe_int(edge.get("reaction_count"))
            if cnt is None and isinstance(node.get("count"), int):
                cnt = node["count"]
            if isinstance(rtype, str) and cnt:
                norm_type = rtype.upper()
                by_type[norm_type] = by_type.get(norm_type, 0) + cnt

    if total == 0 and by_type:
        total = sum(by_type.values())
    return ReactionSummary(total=total, by_type=by_type)


def _fb_user_ref(raw: Any) -> UserRef:
    if not isinstance(raw, dict):
        return UserRef()
    uid = str(raw.get("id") or "") or None
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    url = raw.get("url") if isinstance(raw.get("url"), str) else None
    return UserRef(user_id=uid, full_name=name, profile_url=url)


def _parse_fb_comment_node(node: dict[str, Any]) -> Comment:
    cid = str(node.get("id") or node.get("legacy_fbid") or "") or None
    author = _fb_user_ref(node.get("author"))
    body = node.get("body")
    text = body.get("text") if isinstance(body, dict) and isinstance(body.get("text"), str) else ""
    ts = _safe_int(node.get("created_time"))

    comment_feedback = node.get("feedback") if isinstance(node.get("feedback"), dict) else {}
    reactions = _extract_fb_reactions(comment_feedback)
    if reactions.total == 0:
        crc = node.get("comment_reaction_count")
        if isinstance(crc, dict) and _safe_int(crc.get("count")):
            reactions = ReactionSummary(total=_safe_int(crc.get("count")) or 0)

    attached_media: list[MediaItem] = []
    stickers: list[str] = []
    for att in node.get("attachments", []) if isinstance(node.get("attachments"), list) else []:
        if not isinstance(att, dict):
            continue
        media_obj = att.get("media") if isinstance(att.get("media"), dict) else att
        if isinstance(media_obj, dict):
            for key in ("image", "viewer_image", "photo_image"):
                img = media_obj.get(key)
                if isinstance(img, dict) and isinstance(img.get("uri"), str):
                    attached_media.append(MediaItem(media_type="image", original_uri=img["uri"]))
                    break
            sticker = media_obj.get("sticker") if isinstance(media_obj.get("sticker"), dict) else None
            if sticker and isinstance(sticker.get("uri"), str):
                stickers.append(sticker["uri"])

    raw_replies: list[dict[str, Any]] = []
    reply_containers: list[Any] = [
        comment_feedback.get("display_comments"),
        comment_feedback.get("replies_connection"),
        node.get("replies"),
    ]
    for cri_key in ("comment_rendering_instance", "comment_rendering_instance_for_feed_location"):
        cri_obj = comment_feedback.get(cri_key)
        if isinstance(cri_obj, dict):
            reply_containers.append(cri_obj.get("comments"))
    for container in reply_containers:
        src = container.get("edges") if isinstance(container, dict) else None
        if isinstance(src, list):
            for edge in src:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    raw_replies.append(edge["node"])
    if isinstance(node.get("child_comments"), list):
        raw_replies.extend(c for c in node["child_comments"] if isinstance(c, dict))

    replies = _merge_fb_comments([_parse_fb_comment_node(r) for r in raw_replies])
    like_cnt = reactions.total or None
    if like_cnt is None and isinstance(comment_feedback.get("reactors"), dict):
        like_cnt = _safe_int(comment_feedback["reactors"].get("count")) or None
    return Comment(
        comment_id=cid,
        author=author,
        text=text,
        created_at=_ts_to_dt(ts),
        timestamp_unix=ts,
        like_count=like_cnt,
        media=attached_media,
        stickers=stickers,
        reactions=reactions if reactions.total > 0 or reactions.by_type else None,
        replies=replies,
    )


def _merge_fb_comments(comments: Sequence[Comment]) -> list[Comment]:
    by_key: dict[tuple[str, ...], Comment] = {}
    for c in comments:
        key = (
            ("id", c.comment_id)
            if c.comment_id
            else ("fallback", c.author.full_name or "", str(c.timestamp_unix or ""), c.text)
        )
        if key not in by_key:
            by_key[key] = c
        else:
            prev = by_key[key]
            merged_replies = _merge_fb_comments([*prev.replies, *c.replies])
            best_rx = prev.reactions if (prev.reactions and prev.reactions.total >= (c.reactions.total if c.reactions else 0)) else c.reactions
            by_key[key] = Comment(
                comment_id=prev.comment_id or c.comment_id,
                author=prev.author if prev.author.full_name else c.author,
                text=prev.text or c.text,
                created_at=prev.created_at or c.created_at,
                timestamp_unix=prev.timestamp_unix or c.timestamp_unix,
                like_count=max([v for v in (prev.like_count, c.like_count) if v is not None], default=None),
                media=prev.media or c.media,
                stickers=prev.stickers or c.stickers,
                reactions=best_rx,
                replies=merged_replies,
            )

    out = list(by_key.values())
    out.sort(key=lambda item: item.timestamp_unix if item.timestamp_unix is not None else 0)
    return out


def _extract_comments_from_feedback(feedback: Any) -> list[Comment]:
    if not isinstance(feedback, dict):
        return []
    comments: list[Comment] = []

    candidates: list[Any] = [feedback.get("display_comments"), feedback.get("comments")]
    for cri_key in ("comment_rendering_instance", "comment_rendering_instance_for_feed_location"):
        cri = feedback.get(cri_key)
        if isinstance(cri, dict):
            candidates.append(cri.get("comments"))

    for container in candidates:
        if isinstance(container, dict) and isinstance(container.get("edges"), list):
            for edge in container["edges"]:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    comments.append(_parse_fb_comment_node(edge["node"]))
    return comments


def _extract_fb_media_from_attachment(
    post_id: str,
    media_obj: dict[str, Any],
    candidates: list[FbMediaDownloadCandidate],
) -> None:
    media_id = str(media_obj.get("id") or "") or None
    is_video = bool(
        media_obj.get("__typename") == "Video"
        or media_obj.get("playable_url")
        or media_obj.get("playable_url_quality_hd")
        or media_obj.get("browser_native_hd_url")
        or media_obj.get("dash_manifest")
        or media_obj.get("video_dash_manifest")
    )
    if is_video:
        prog = (
            media_obj.get("playable_url_quality_hd")
            or media_obj.get("browser_native_hd_url")
            or media_obj.get("playable_url")
            or media_obj.get("browser_native_sd_url")
        )
        dash_xml = media_obj.get("dash_manifest") or media_obj.get("video_dash_manifest") or ""
        dash_v, dash_a = parse_dash_manifest(dash_xml) if isinstance(dash_xml, str) else (None, None)
        if prog or dash_v:
            candidates.append(
                FbMediaDownloadCandidate(
                    post_id=post_id,
                    media_id=media_id,
                    media_type="video",
                    progressive_url=prog if isinstance(prog, str) else None,
                    dash_video_url=dash_v,
                    dash_audio_url=dash_a,
                )
            )
        return

    img_uri: str | None = None
    width: int | None = None
    height: int | None = None
    for key in ("viewer_image", "photo_image", "image"):
        img = media_obj.get(key)
        if isinstance(img, dict) and isinstance(img.get("uri"), str):
            img_uri = img["uri"]
            width = _safe_int(img.get("width"))
            height = _safe_int(img.get("height"))
            break
    if img_uri:
        candidates.append(
            FbMediaDownloadCandidate(
                post_id=post_id,
                media_id=media_id,
                media_type="image",
                progressive_url=img_uri,
                width=width,
                height=height,
            )
        )


def _pick_fb_text(val: Any) -> str | None:
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, dict) and isinstance(val.get("text"), str):
        return val["text"].strip() or None
    return None


def _is_fb_internal_media_url(url: str) -> bool:
    dom = extract_domain(url) or ""
    if not (dom == "facebook.com" or dom.endswith(".facebook.com") or dom == "fb.com"):
        return False
    low = url.lower()
    return any(seg in low for seg in ("/photo", "/videos/", "/reel/", "/watch", "/posts/"))


def _extract_link_card_from_attachment(
    att: dict[str, Any],
) -> tuple[SharedPostRef, str | None] | None:
    """Extract external link card (URL, title, description, domain, thumbnail URL) from a Comet attachment."""
    nodes: list[dict[str, Any]] = [att]
    styles = att.get("styles")
    if isinstance(styles, dict) and isinstance(styles.get("attachment"), dict):
        satt = styles["attachment"]
        nodes.append(satt)
        renderer = satt.get("story_attachment_link_renderer")
        if isinstance(renderer, dict) and isinstance(renderer.get("attachment"), dict):
            nodes.append(renderer["attachment"])

    # Do not treat regular photo/video uploads or albums as external link cards.
    for node in nodes:
        media_obj = node.get("media")
        if isinstance(media_obj, dict) and (
            media_obj.get("__typename") in ("Photo", "Video")
            or media_obj.get("album")
            or media_obj.get("playable_url")
            or media_obj.get("playable_url_quality_hd")
            or media_obj.get("dash_manifest")
            or media_obj.get("video_dash_manifest")
        ):
            return None

    clean_url: str | None = None
    title: str | None = None
    desc: str | None = None
    source_name: str | None = None
    thumb_url: str | None = None

    for node in nodes:
        raw_candidates: list[Any] = [node.get("url")]
        if isinstance(node.get("target"), dict):
            raw_candidates.extend([node["target"].get("external_url"), node["target"].get("url")])
        if isinstance(node.get("action_links"), list) and node["action_links"]:
            first_act = node["action_links"][0]
            if isinstance(first_act, dict):
                raw_candidates.append(first_act.get("url"))

        for raw_u in raw_candidates:
            if not clean_url and isinstance(raw_u, str):
                clean_url = clean_fb_external_url(raw_u)

        title = title or _pick_fb_text(node.get("title_with_entities")) or _pick_fb_text(node.get("title"))
        desc = desc or _pick_fb_text(node.get("description")) or _pick_fb_text(node.get("subtitle"))
        source_name = source_name or _pick_fb_text(node.get("source"))

        if not thumb_url and isinstance(node.get("media"), dict):
            m = node["media"]
            for img_key in ("image", "photo_image", "viewer_image"):
                img = m.get(img_key)
                if isinstance(img, dict) and isinstance(img.get("uri"), str) and img["uri"]:
                    thumb_url = img["uri"]
                    break

    if not clean_url:
        return None
    if _is_fb_internal_media_url(clean_url) and not any((title, desc, source_name)):
        return None

    dom = extract_domain(clean_url)
    if dom in ("facebook.com", "fb.com") and source_name:
        dom = source_name
    ref = SharedPostRef(
        url=clean_url,
        title=title,
        text=desc,
        domain=dom or source_name,
    )
    return ref, thumb_url


def _merge_shared_post_refs(
    first: SharedPostRef | None,
    second: SharedPostRef | None,
) -> SharedPostRef | None:
    if first is None:
        return second
    if second is None:
        return first
    return SharedPostRef(
        url=first.url or second.url,
        author_name=first.author_name or second.author_name,
        text=first.text or second.text,
        title=first.title or second.title,
        domain=first.domain or second.domain,
        thumbnail_local_path=first.thumbnail_local_path or second.thumbnail_local_path,
    )


def _find_story_feedbacks(story: dict[str, Any]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    if isinstance(story.get("feedback"), dict):
        roots.append(story["feedback"])
    comet = story.get("comet_sections")
    if isinstance(comet, dict):
        fb_sec = comet.get("feedback")
        if isinstance(fb_sec, dict):
            roots.append(fb_sec)
        content_story = (
            (comet.get("content") or {}).get("story")
            if isinstance(comet.get("content"), dict)
            else None
        )
        if isinstance(content_story, dict) and isinstance(content_story.get("feedback"), dict):
            roots.append(content_story["feedback"])

    feedbacks: list[dict[str, Any]] = []
    comment_list_keys = frozenset({"display_comments", "comments", "replies", "child_comments"})
    for root in roots:
        for d in _walk_filtered_fb_dicts(root, comment_list_keys):
            if any(
                k in d
                for k in (
                    "reaction_count",
                    "i18n_reaction_count",
                    "top_reactions",
                    "display_comments",
                    "comments",
                    "comment_rendering_instance",
                    "comment_rendering_instance_for_feed_location",
                )
            ):
                feedbacks.append(d)
    return feedbacks


def _merge_fb_reactions(summaries: Sequence[ReactionSummary]) -> ReactionSummary:
    total = 0
    by_type: dict[str, int] = {}
    for rx in summaries:
        if rx.total > total:
            total = rx.total
        for rtype, cnt in rx.by_type.items():
            if cnt > by_type.get(rtype, 0):
                by_type[rtype] = cnt
    if total == 0 and by_type:
        total = sum(by_type.values())
    return ReactionSummary(total=total, by_type=by_type)


def _extract_attached_story_ref(
    d: dict[str, Any],
    content_story: dict[str, Any],
) -> SharedPostRef | None:
    holders: list[dict[str, Any]] = []
    for cand in (d.get("attached_story"), content_story.get("attached_story")):
        if isinstance(cand, dict):
            holders.append(cand)
    if not holders:
        return None

    att_url: str | None = None
    att_author: str | None = None
    att_text: str | None = None
    att_title: str | None = None

    for holder in holders:
        for node in _walk_dicts(holder):
            if not att_url:
                for uk in ("permalink_url", "wwwURL", "url"):
                    raw_u = node.get(uk)
                    if isinstance(raw_u, str) and raw_u:
                        cleaned = clean_fb_external_url(raw_u)
                        if cleaned and any(seg in cleaned for seg in ("/posts/", "/groups/", "/videos/", "/reel/", "/photo", "http")):
                            att_url = cleaned
                            break
            if not att_author:
                actors = node.get("actors")
                if isinstance(actors, list) and actors and isinstance(actors[0], dict):
                    name = actors[0].get("name")
                    if isinstance(name, str) and name.strip():
                        att_author = name.strip()
            if not att_text:
                msg = node.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
                    att_text = msg["text"]
            if not att_title and isinstance(node.get("title"), str) and node["title"].strip():
                att_title = node["title"].strip()

    if not any((att_url, att_author, att_text, att_title)):
        return None
    return SharedPostRef(
        url=att_url,
        author_name=att_author,
        text=att_text,
        title=att_title,
        domain=extract_domain(att_url),
    )


def _is_top_fb_story(d: dict[str, Any]) -> bool:
    if d.get("sponsored_data") or d.get("ad_id") or d.get("__typename") in ("SponsoredStory", "PYMKUnit"):
        return False
    if not (d.get("post_id") or d.get("comet_sections") or d.get("__typename") == "Story"):
        return False
    return bool(d.get("post_id") or d.get("id")) and any(
        k in d
        for k in (
            "comet_sections",
            "creation_time",
            "message",
            "attachments",
            "feedback",
            "attached_story",
        )
    )


def _walk_top_fb_stories(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        if _is_top_fb_story(obj):
            yield obj
            return
        for key, val in obj.items():
            if key in _EXCLUDED_FB_STORY_CONTAINERS:
                continue
            yield from _walk_top_fb_stories(val)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_top_fb_stories(item)


def extract_fb_posts_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> tuple[list[Post], list[FbMediaDownloadCandidate]]:
    """Extract normalized Facebook Posts and media download candidates from GraphQL/SSR payloads."""
    posts_by_id: dict[str, Post] = {}
    candidates_by_post: dict[str, list[FbMediaDownloadCandidate]] = {}

    for payload in payloads:
        for d in _walk_top_fb_stories(payload):

            post_id = str(d.get("post_id") or d.get("id") or "")
            if not post_id:
                continue

            comet = d.get("comet_sections") if isinstance(d.get("comet_sections"), dict) else {}
            content_story = (
                (comet.get("content") or {}).get("story")
                if isinstance(comet.get("content"), dict)
                else None
            )
            if not isinstance(content_story, dict):
                content_story = d

            context_story = (
                (comet.get("context_layout") or {}).get("story")
                if isinstance(comet.get("context_layout"), dict)
                else None
            )
            if not isinstance(context_story, dict):
                context_story = d

            url = (
                d.get("permalink_url")
                or d.get("url")
                or content_story.get("permalink_url")
                or context_story.get("url")
            )
            ts = _safe_int(
                d.get("creation_time")
                or content_story.get("creation_time")
                or context_story.get("creation_time")
                or (
                    ((comet.get("timestamp") or {}).get("story") or {}).get("creation_time")
                    if isinstance(comet.get("timestamp"), dict)
                    else None
                )
            )

            actors = (
                d.get("actors")
                or content_story.get("actors")
                or context_story.get("actors")
                or []
            )
            author = _fb_user_ref(actors[0]) if isinstance(actors, list) and actors else UserRef()

            caption = ""
            for msg_holder in (content_story, d):
                msg = msg_holder.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("text"), str):
                    caption = msg["text"]
                    break
            hashtags = list(dict.fromkeys(_HASHTAG_RE.findall(caption)))

            audience: str | None = None
            priv = d.get("privacy_scope") or context_story.get("privacy_scope")
            if isinstance(priv, dict):
                audience = priv.get("label") or priv.get("description")

            album_name: str | None = (
                d.get("album_name") if isinstance(d.get("album_name"), str) else None
            )

            shared_post = _extract_attached_story_ref(d, content_story)

            feedbacks = _find_story_feedbacks(d)
            reactions = _merge_fb_reactions([_extract_fb_reactions(fb) for fb in feedbacks])
            all_fb_comments: list[Comment] = []
            for fb in feedbacks:
                all_fb_comments.extend(_extract_comments_from_feedback(fb))
            comments = _merge_fb_comments(all_fb_comments)

            post_candidates: list[FbMediaDownloadCandidate] = []
            attachments = (
                d.get("attachments")
                if isinstance(d.get("attachments"), list)
                else content_story.get("attachments")
            )
            if isinstance(attachments, list):
                for att in attachments:
                    if not isinstance(att, dict):
                        continue
                    styles_obj = att.get("styles")
                    styles_att = (
                        styles_obj.get("attachment")
                        if isinstance(styles_obj, dict) and isinstance(styles_obj.get("attachment"), dict)
                        else None
                    )
                    att_holders: list[dict[str, Any]] = []
                    if styles_att is not None:
                        att_holders.append(styles_att)
                    att_holders.append(att)

                    sub_nodes: list[dict[str, Any]] = []
                    for holder in att_holders:
                        for sub_key in ("all_subattachments", "subattachments"):
                            sub = holder.get(sub_key)
                            if isinstance(sub, dict) and isinstance(sub.get("nodes"), list):
                                sub_nodes.extend(n for n in sub["nodes"] if isinstance(n, dict))
                        if sub_nodes:
                            break

                    if sub_nodes:
                        for sub_node in sub_nodes:
                            sub_styles = sub_node.get("styles")
                            sub_styles_att = (
                                sub_styles.get("attachment")
                                if isinstance(sub_styles, dict)
                                and isinstance(sub_styles.get("attachment"), dict)
                                else None
                            )
                            sub_media_candidates: list[dict[str, Any]] = []
                            if sub_styles_att is not None and isinstance(sub_styles_att.get("media"), dict):
                                sub_media_candidates.append(sub_styles_att["media"])
                            if isinstance(sub_node.get("media"), dict):
                                sub_media_candidates.append(sub_node["media"])
                            if not sub_media_candidates:
                                sub_media_candidates.append(sub_node)
                            for m in sub_media_candidates:
                                if not album_name and isinstance(m.get("album"), dict):
                                    album_name = m["album"].get("name")
                                before = len(post_candidates)
                                _extract_fb_media_from_attachment(post_id, m, post_candidates)
                                if len(post_candidates) > before:
                                    break
                        continue

                    link_info = _extract_link_card_from_attachment(att)
                    if link_info is not None:
                        link_ref, thumb_url = link_info
                        shared_post = _merge_shared_post_refs(shared_post, link_ref)
                        if thumb_url:
                            post_candidates.append(
                                FbMediaDownloadCandidate(
                                    post_id=post_id,
                                    media_id=None,
                                    media_type="image",
                                    progressive_url=thumb_url,
                                    is_link_thumbnail=True,
                                )
                            )
                        continue

                    for holder in att_holders:
                        if isinstance(holder.get("media"), dict):
                            m = holder["media"]
                            if not album_name and isinstance(m.get("album"), dict):
                                album_name = m["album"].get("name")
                            before = len(post_candidates)
                            _extract_fb_media_from_attachment(post_id, m, post_candidates)
                            if len(post_candidates) > before:
                                break

            regular_candidates = [c for c in post_candidates if not c.is_link_thumbnail]
            if album_name:
                post_type: Literal["post", "carousel", "reel", "album_photo", "shared", "other"] = "album_photo"
            elif shared_post is not None and not regular_candidates:
                post_type = "shared"
            elif len(regular_candidates) > 1:
                post_type = "carousel"
            else:
                post_type = "post"

            new_post = Post(
                platform="facebook",
                post_id=post_id,
                url=url if isinstance(url, str) else None,
                post_type=post_type,
                author=author,
                created_at=_ts_to_dt(ts),
                timestamp_unix=ts,
                caption=caption,
                hashtags=hashtags,
                likes=LikesInfo(count=reactions.total or None),
                reactions=reactions,
                comments=comments,
                album_name=album_name,
                shared_post=shared_post,
                audience=audience,
                source="playwright",
            )

            if post_id not in posts_by_id:
                posts_by_id[post_id] = new_post
                candidates_by_post[post_id] = post_candidates
            else:
                prev = posts_by_id[post_id]
                merged_comments = _merge_fb_comments([*prev.comments, *new_post.comments])
                best_rx = prev.reactions if prev.reactions.total >= new_post.reactions.total else new_post.reactions
                posts_by_id[post_id] = prev.model_copy(
                    update={
                        "caption": prev.caption or new_post.caption,
                        "url": prev.url or new_post.url,
                        "created_at": prev.created_at or new_post.created_at,
                        "timestamp_unix": prev.timestamp_unix or new_post.timestamp_unix,
                        "reactions": best_rx,
                        "likes": LikesInfo(count=best_rx.total or None),
                        "comments": merged_comments,
                        "album_name": prev.album_name or new_post.album_name,
                        "shared_post": _merge_shared_post_refs(prev.shared_post, new_post.shared_post),
                    }
                )
                if not candidates_by_post[post_id] and post_candidates:
                    candidates_by_post[post_id] = post_candidates

    all_candidates: list[FbMediaDownloadCandidate] = []
    for pid in posts_by_id:
        all_candidates.extend(candidates_by_post[pid])
    return list(posts_by_id.values()), all_candidates


def _click_first_visible(page: Any, labels: Sequence[str], roles: Sequence[str]) -> bool:
    for label in labels:
        for role in roles:
            try:
                locator = page.get_by_role(role, name=label)
                count = locator.count()
            except Exception:
                continue
            for idx in range(count):
                item = locator.nth(idx)
                try:
                    if item.is_visible():
                        item.click()
                        return True
                except Exception:
                    continue

        try:
            text_loc = page.get_by_text(label)
            count = text_loc.count()
        except Exception:
            continue
        for idx in range(count):
            item = text_loc.nth(idx)
            try:
                if item.is_visible():
                    item.click()
                    return True
            except Exception:
                continue

    return False


def switch_to_all_comments(
    page: Any,
    cooldown: CooldownTracker,
    max_switches: int = 10,
) -> int:
    """Switch Facebook comment filter from 'Most relevant' to 'All comments'."""
    switched = 0
    while switched < max_switches and _click_first_visible(page, _RELEVANT_SORT_LABELS, ("button",)):
        cooldown.wait_step()
        if _click_first_visible(
            page,
            _ALL_COMMENTS_LABELS,
            ("menuitem", "menuitemradio", "option", "button"),
        ):
            switched += 1
            cooldown.wait_step()
        else:
            break
    return switched


def _click_first_visible_regex(
    page: Any,
    pattern: re.Pattern[str],
    roles: Sequence[str],
) -> bool:
    for role in roles:
        try:
            locator = page.get_by_role(role, name=pattern)
            count = locator.count()
        except Exception:
            continue
        for idx in range(count):
            item = locator.nth(idx)
            try:
                if item.is_visible():
                    item.click()
                    return True
            except Exception:
                continue
    return False


def expand_fb_comments(
    page: Any,
    cooldown: CooldownTracker,
    max_clicks: int = 20,
) -> int:
    """Click visible Facebook expand/reply comment buttons."""
    clicks = 0
    while clicks < max_clicks:
        if _click_first_visible(page, _FB_EXPAND_LABELS, ("button",)) or _click_first_visible_regex(
            page, _FB_EXPAND_REGEX, ("button",)
        ):
            clicks += 1
            cooldown.wait_step()
        else:
            break
    return clicks


def extract_fb_comment_counts_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> dict[str, int]:
    """Extract {post_id: expected_total_comments} from Facebook timeline Story UFI payloads."""
    counts: dict[str, int] = {}
    for payload in payloads:
        for st in _walk_top_fb_stories(payload):
            pid = str(st.get("post_id") or st.get("id") or "").strip()
            if not pid:
                continue
            max_cnt = counts.get(pid, 0)
            for sub in _walk_dicts(st):
                ccsr = sub.get("comments_count_summary_renderer")
                if isinstance(ccsr, dict):
                    fb_obj = ccsr.get("feedback")
                    if isinstance(fb_obj, dict):
                        for cri_key in ("comment_rendering_instance", "comment_rendering_instance_for_feed_location"):
                            cri = fb_obj.get(cri_key)
                            if isinstance(cri, dict) and isinstance(cri.get("comments"), dict):
                                tc = _safe_int(cri["comments"].get("total_count"))
                                if tc is not None and tc > max_cnt:
                                    max_cnt = tc
                for cri_key in ("comment_rendering_instance", "comment_rendering_instance_for_feed_location"):
                    cri = sub.get(cri_key)
                    if isinstance(cri, dict) and isinstance(cri.get("comments"), dict):
                        tc = _safe_int(cri["comments"].get("total_count"))
                        if tc is not None and tc > max_cnt:
                            max_cnt = tc
                for key in ("comments", "display_comments", "total_comment_count"):
                    val = sub.get(key)
                    if isinstance(val, int) and not isinstance(val, bool) and val > max_cnt:
                        max_cnt = val
                    elif isinstance(val, dict):
                        for ck in ("total_count", "count"):
                            tc = _safe_int(val.get(ck))
                            if tc is not None and tc > max_cnt:
                                max_cnt = tc
            if max_cnt > 0:
                counts[pid] = max_cnt
    return counts


def extract_fb_timeline_has_next_page(
    payloads: Sequence[dict[str, Any]],
) -> bool | None:
    """Return the latest timeline pagination `has_next_page` flag found in payloads, or None."""
    result: bool | None = None
    for payload in payloads:
        if isinstance(payload, dict):
            label = payload.get("label")
            if isinstance(label, str) and "timeline" in label.lower():
                data = payload.get("data")
                if isinstance(data, dict) and isinstance(data.get("page_info"), dict):
                    hnp = data["page_info"].get("has_next_page")
                    if isinstance(hnp, bool):
                        result = hnp
        for d in _walk_dicts(payload):
            tl = d.get("timeline_list_feed_units")
            if isinstance(tl, dict) and isinstance(tl.get("page_info"), dict):
                hnp = tl["page_info"].get("has_next_page")
                if isinstance(hnp, bool):
                    result = hnp
    return result


def extract_fb_comments_and_reactions_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    post_id: str | None = None,
) -> tuple[list[Comment], ReactionSummary]:
    """Extract all comments (incl. replies) and reactions from Story and standalone Feedback payloads."""
    all_comments: list[Comment] = []
    rx_list: list[ReactionSummary] = []

    story_posts, _ = extract_fb_posts_from_payloads(payloads)
    for sp in story_posts:
        if post_id and sp.post_id != post_id and len(story_posts) > 1:
            continue
        all_comments.extend(sp.comments)
        if sp.reactions.total > 0 or sp.reactions.by_type:
            rx_list.append(sp.reactions)

    # Also extract standalone Feedback / Comment-replies GraphQL responses (2026 Comet)
    for payload in payloads:
        for d in _walk_dicts(payload):
            tn = d.get("__typename")
            if tn == "Feedback" or any(
                k in d
                for k in (
                    "comment_rendering_instance_for_feed_location",
                    "comment_rendering_instance",
                    "display_comments",
                )
            ):
                extracted = _extract_comments_from_feedback(d)
                if extracted:
                    all_comments.extend(extracted)
                rx = _extract_fb_reactions(d)
                if rx.total > 0 or rx.by_type:
                    rx_list.append(rx)
            elif tn == "Comment" and isinstance(d.get("feedback"), dict):
                fb_obj = d["feedback"]
                if isinstance(fb_obj.get("replies_connection"), dict) and isinstance(
                    fb_obj["replies_connection"].get("edges"), list
                ):
                    if d.get("id") and (d.get("body") or d.get("author") or fb_obj["replies_connection"]["edges"]):
                        all_comments.append(_parse_fb_comment_node(d))

    # Filter out empty stub comments (e.g. parent_object_ent stubs with no author/text/media/replies)
    cleaned = [
        c
        for c in _merge_fb_comments(all_comments)
        if c.text or c.media or c.stickers or c.author.full_name or c.replies
    ]
    return cleaned, _merge_fb_reactions(rx_list)


_ALBUM_URL_RE = re.compile(
    r"(?:https?://(?:www\.|m\.)?facebook\.com)?(/media/set/\?[^\"'\s<>]+)",
    re.IGNORECASE,
)


def extract_fb_profile_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> tuple[Profile | None, str | None]:
    """Extract the owner's Facebook Profile and avatar URL from `/me` GraphQL/SSR payloads."""
    user_id: str | None = None
    username: str | None = None
    full_name: str | None = None
    avatar_url: str | None = None

    def _consume_actor(act: dict[str, Any], *, require_matching_id: bool = False) -> None:
        nonlocal user_id, username, full_name, avatar_url
        act_id = str(act.get("id") or "") or None
        if require_matching_id and user_id and act_id and act_id != user_id:
            return
        if not user_id and act_id:
            user_id = act_id
        if not full_name and isinstance(act.get("name"), str) and act["name"].strip():
            full_name = act["name"].strip()
        raw_url = act.get("url")
        if not username and isinstance(raw_url, str) and "facebook.com/" in raw_url:
            path = urllib.parse.urlparse(raw_url).path.strip("/")
            if path and "/" not in path and path not in {"me", "profile.php", "people"}:
                username = path
        for k in ("profilePicLarge", "profile_picture", "profile_pic"):
            sub = act.get(k)
            if isinstance(sub, dict) and isinstance(sub.get("uri"), str) and sub["uri"]:
                if not avatar_url or k == "profilePicLarge":
                    avatar_url = sub["uri"]
                break

    # Pass 1: prefer profile_header_renderer.user (contains url + profilePicLarge) and profile_owner.
    for payload in payloads:
        for d in _walk_dicts(payload):
            phr = d.get("profile_header_renderer")
            if isinstance(phr, dict) and isinstance(phr.get("user"), dict):
                _consume_actor(phr["user"])
            if isinstance(d.get("profile_owner"), dict):
                _consume_actor(d["profile_owner"], require_matching_id=True)

    # Pass 2: enrich missing fields (username / avatar) from viewer.actor or matching Story.actors[0].
    if not all((user_id, username, full_name, avatar_url)):
        for payload in payloads:
            for d in _walk_dicts(payload):
                viewer = d.get("viewer")
                if isinstance(viewer, dict) and isinstance(viewer.get("actor"), dict):
                    _consume_actor(viewer["actor"], require_matching_id=True)

    if not all((user_id, username, full_name, avatar_url)):
        for payload in payloads:
            for d in _walk_top_fb_stories(payload):
                actors = d.get("actors")
                if isinstance(actors, list) and actors and isinstance(actors[0], dict):
                    _consume_actor(actors[0], require_matching_id=bool(user_id))

    if not any((user_id, username, full_name, avatar_url)):
        return None, None
    return (
        Profile(
            platform="facebook",
            user_id=user_id,
            username=username,
            full_name=full_name,
        ),
        avatar_url,
    )


def extract_fb_album_urls(payloads: Sequence[dict[str, Any]], html: str = "") -> list[str]:
    """Extract unique Facebook album set URLs from `/me/photos_albums` payloads and HTML."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(raw: str) -> None:
        unescaped = html_lib.unescape(raw.strip())
        m = _ALBUM_URL_RE.search(unescaped)
        if not m:
            return
        full = f"https://www.facebook.com{m.group(1)}"
        if full not in seen:
            seen.add(full)
            ordered.append(full)

    if html:
        for m in _ALBUM_URL_RE.finditer(html_lib.unescape(html)):
            _add(m.group(0))

    for payload in payloads:
        for d in _walk_dicts(payload):
            for key in ("url", "album_url", "href"):
                val = d.get(key)
                if isinstance(val, str) and "/media/set/" in val:
                    _add(val)

    return ordered


@dataclass(frozen=True)
class FbAlbumPhotoTarget:
    """Single photo discovered inside a Facebook album `grid_media` or `GenericMediaSet`."""

    photo_id: str
    photo_url: str
    album_name: str | None = None
    fallback_image_url: str | None = None
    owner_id: str | None = None


def _extract_album_set_key(url: str) -> str | None:
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:
        return None
    vals = qs.get("set")
    if vals and vals[0].strip():
        return vals[0].strip()
    return None


def _lookup_album_title(album_titles: dict[str, str], album_url: str) -> str | None:
    set_key = _extract_album_set_key(album_url)
    if not set_key:
        return None
    if set_key in album_titles:
        return album_titles[set_key]
    if set_key.startswith("a.") and set_key[2:] in album_titles:
        return album_titles[set_key[2:]]
    return None


def extract_fb_album_titles(payloads: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Extract album id/set -> album title mapping from `/me/photos_albums` or album page payloads."""
    titles: dict[str, str] = {}

    def _record(key: str | None, title: str | None) -> None:
        if not key or not title:
            return
        titles.setdefault(key, title)
        if key.startswith("a."):
            titles.setdefault(key[2:], title)
        elif key.isdigit():
            titles.setdefault(f"a.{key}", title)

    for payload in payloads:
        for d in _walk_dicts(payload):
            tn = d.get("__typename")
            if tn == "TimelineAppCollectionItem":
                raw_url = d.get("url")
                if isinstance(raw_url, str) and "/media/set/" in raw_url:
                    title = _pick_fb_text(d.get("title"))
                    set_key = _extract_album_set_key(html_lib.unescape(raw_url))
                    _record(set_key, title)
                    node = d.get("node")
                    if isinstance(node, dict) and node.get("id"):
                        _record(str(node["id"]), title)
            elif tn in ("Album", "GenericMediaSet", "PhotoAlbum", "MediaSet"):
                title = _pick_fb_text(d.get("title")) or _pick_fb_text(d.get("name"))
                raw_url = d.get("url")
                if isinstance(raw_url, str) and "/media/set/" in raw_url:
                    _record(_extract_album_set_key(html_lib.unescape(raw_url)), title)
                if d.get("id"):
                    _record(str(d["id"]), title)

    return titles


def extract_fb_album_photo_targets(
    payloads: Sequence[dict[str, Any]],
    *,
    album_url: str = "",
    album_name: str | None = None,
) -> list[FbAlbumPhotoTarget]:
    """Extract unique Photo targets from Facebook album `grid_media` and `GenericMediaSet` payloads."""
    set_key = _extract_album_set_key(album_url)
    resolved_title = album_name
    if not resolved_title:
        page_titles = extract_fb_album_titles(payloads)
        resolved_title = _lookup_album_title(page_titles, album_url) or next(
            iter(page_titles.values()), None
        )

    targets: list[FbAlbumPhotoTarget] = []
    seen_ids: set[str] = set()

    def _consume_edges(edges: Any, local_title: str | None) -> None:
        if not isinstance(edges, list):
            return
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict):
                continue
            if node.get("__typename") != "Photo" and node.get("__isMedia") != "Photo":
                continue
            pid = str(node.get("id") or "").strip()
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            fallback_uri: str | None = None
            for img_key in ("viewer_image", "photo_image", "image"):
                img = node.get(img_key)
                if isinstance(img, dict) and isinstance(img.get("uri"), str) and img["uri"]:
                    fallback_uri = img["uri"]
                    break
            owner = node.get("owner")
            owner_id = str(owner.get("id") or "") or None if isinstance(owner, dict) else None
            raw_url = node.get("url")
            if isinstance(raw_url, str) and "/photo" in raw_url:
                photo_url = html_lib.unescape(raw_url)
            elif set_key:
                photo_url = f"https://www.facebook.com/photo/?fbid={pid}&set={set_key}"
            else:
                photo_url = f"https://www.facebook.com/photo/?fbid={pid}"
            targets.append(
                FbAlbumPhotoTarget(
                    photo_id=pid,
                    photo_url=photo_url,
                    album_name=local_title or resolved_title,
                    fallback_image_url=fallback_uri,
                    owner_id=owner_id,
                )
            )

    for payload in payloads:
        for d in _walk_dicts(payload):
            local_title = _pick_fb_text(d.get("title")) or _pick_fb_text(d.get("name")) or resolved_title
            gm = d.get("grid_media")
            if isinstance(gm, dict):
                _consume_edges(gm.get("edges"), local_title)
            if d.get("__typename") in ("GenericMediaSet", "Album", "MediaSet"):
                media_conn = d.get("media")
                if isinstance(media_conn, dict):
                    _consume_edges(media_conn.get("edges"), local_title)

    return targets


def extract_fb_photo_post_from_payloads(
    payloads: Sequence[dict[str, Any]],
    *,
    photo_id: str,
    photo_url: str | None = None,
    album_name: str | None = None,
    fallback_image_url: str | None = None,
) -> tuple[Post | None, list[FbMediaDownloadCandidate]]:
    """Extract a normalized Facebook album Photo post from `/photo/?fbid=...` SSR/GraphQL payloads."""
    story_posts, story_cands = extract_fb_posts_from_payloads(payloads)
    cands_by_story: dict[str, list[FbMediaDownloadCandidate]] = {}
    for c in story_cands:
        cands_by_story.setdefault(c.post_id, []).append(c)

    matched_story: Post | None = None
    creation_story_ids: set[str] = set()
    best_img_uri: str | None = None
    best_img_area: int = -1
    best_img_w: int | None = None
    best_img_h: int | None = None
    photo_ts: int | None = None
    photo_caption: str = ""
    photo_author: UserRef | None = None
    photo_album: str | None = album_name
    photo_url_found: str | None = photo_url
    feedback_nodes: list[dict[str, Any]] = []

    def _consider_image_holder(obj: dict[str, Any]) -> None:
        nonlocal best_img_uri, best_img_area, best_img_w, best_img_h
        for key in ("viewer_image", "photo_image", "image"):
            img = obj.get(key)
            if not isinstance(img, dict):
                continue
            uri = img.get("uri")
            if not isinstance(uri, str) or not uri:
                continue
            w = _safe_int(img.get("width"))
            h = _safe_int(img.get("height"))
            area = (w * h) if (w and h) else 0
            if best_img_uri is None or area > best_img_area:
                best_img_uri = uri
                best_img_area = area
                best_img_w = w
                best_img_h = h

    def _collect_feedbacks_under(root: dict[str, Any]) -> None:
        comment_list_keys = frozenset({"display_comments", "comments", "replies", "child_comments"})
        for sub in _walk_filtered_fb_dicts(root, comment_list_keys):
            if any(
                k in sub
                for k in (
                    "reaction_count",
                    "i18n_reaction_count",
                    "top_reactions",
                    "display_comments",
                    "comments",
                    "comment_rendering_instance",
                    "comment_rendering_instance_for_feed_location",
                )
            ):
                feedback_nodes.append(sub)

    for payload in payloads:
        for d in _walk_dicts(payload):
            # Capture currMedia or matching Photo node
            curr = d.get("currMedia")
            if isinstance(curr, dict) and (not curr.get("id") or str(curr.get("id")) == photo_id):
                _consider_image_holder(curr)
                if photo_ts is None:
                    photo_ts = _safe_int(curr.get("created_time") or curr.get("creation_time"))
                if photo_author is None and isinstance(curr.get("owner"), dict):
                    photo_author = _fb_user_ref(curr["owner"])

            is_matching_photo = (
                (d.get("__typename") == "Photo" or d.get("__isMedia") == "Photo")
                and str(d.get("id") or "") == photo_id
            )
            if is_matching_photo:
                _consider_image_holder(d)
                if photo_ts is None:
                    photo_ts = _safe_int(d.get("created_time") or d.get("creation_time"))
                if not photo_caption:
                    msg = d.get("message")
                    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
                        photo_caption = msg["text"]
                if photo_author is None and isinstance(d.get("owner"), dict):
                    cand_author = _fb_user_ref(d["owner"])
                    if cand_author.full_name or cand_author.user_id:
                        photo_author = cand_author
                if not photo_album and isinstance(d.get("album"), dict):
                    photo_album = _pick_fb_text(d["album"].get("name")) or _pick_fb_text(d["album"].get("title"))
                if isinstance(d.get("url"), str) and "/photo" in d["url"]:
                    photo_url_found = html_lib.unescape(d["url"])
                cs = d.get("creation_story")
                if isinstance(cs, dict):
                    if cs.get("id"):
                        creation_story_ids.add(str(cs["id"]))
                    if photo_ts is None:
                        photo_ts = _safe_int(cs.get("creation_time") or cs.get("created_time"))
                    if not photo_caption:
                        cmsg = cs.get("message")
                        if isinstance(cmsg, dict) and isinstance(cmsg.get("text"), str) and cmsg["text"].strip():
                            photo_caption = cmsg["text"]
                    if (photo_author is None or not photo_author.full_name) and isinstance(cs.get("actors"), list) and cs["actors"]:
                        photo_author = _fb_user_ref(cs["actors"][0])
                    if isinstance(cs.get("feedback"), dict):
                        _collect_feedbacks_under(cs["feedback"])
                if isinstance(d.get("feedback"), dict):
                    _collect_feedbacks_under(d["feedback"])

            # Comet photo viewer root content renderer holds creation_story / feedback
            cpr = d.get("comet_photo_root_content_renderer")
            if isinstance(cpr, dict):
                _collect_feedbacks_under(cpr)
                media_node = cpr.get("media")
                if isinstance(media_node, dict) and (not media_node.get("id") or str(media_node.get("id")) == photo_id):
                    _consider_image_holder(media_node)
                    if photo_ts is None:
                        photo_ts = _safe_int(media_node.get("created_time") or media_node.get("creation_time"))
                    if not photo_caption:
                        mmsg = media_node.get("message")
                        if isinstance(mmsg, dict) and isinstance(mmsg.get("text"), str) and mmsg["text"].strip():
                            photo_caption = mmsg["text"]

    # Match Story if present in the photo page payloads
    for sp in story_posts:
        sp_cands = cands_by_story.get(sp.post_id, [])
        if (
            sp.post_id == photo_id
            or sp.post_id in creation_story_ids
            or any(c.media_id == photo_id for c in sp_cands)
            or (sp.url and photo_id in sp.url)
        ):
            matched_story = sp
            break

    if matched_story is not None:
        for sc in cands_by_story.get(matched_story.post_id, []):
            if not sc.is_link_thumbnail and sc.progressive_url:
                area = (sc.width * sc.height) if (sc.width and sc.height) else 0
                if best_img_uri is None or area > best_img_area:
                    best_img_uri = sc.progressive_url
                    best_img_area = area
                    best_img_w = sc.width
                    best_img_h = sc.height

    if best_img_uri is None and fallback_image_url:
        best_img_uri = fallback_image_url

    photo_reactions = _merge_fb_reactions([_extract_fb_reactions(fb) for fb in feedback_nodes])
    photo_comments_raw: list[Comment] = []
    for fb in feedback_nodes:
        photo_comments_raw.extend(_extract_comments_from_feedback(fb))
    photo_comments = _merge_fb_comments(photo_comments_raw)

    if matched_story is not None:
        reactions = (
            matched_story.reactions
            if matched_story.reactions.total >= photo_reactions.total
            else photo_reactions
        )
        comments = _merge_fb_comments([*matched_story.comments, *photo_comments])
        caption = matched_story.caption or photo_caption
        ts = matched_story.timestamp_unix or photo_ts
        author = (
            matched_story.author
            if (matched_story.author.full_name or matched_story.author.user_id)
            else (photo_author or UserRef())
        )
        final_album = matched_story.album_name or photo_album
        final_url = matched_story.url or photo_url_found
        audience = matched_story.audience
        shared_post = matched_story.shared_post
    else:
        if not any((best_img_uri, photo_ts, photo_caption, photo_comments, photo_reactions.total)):
            return None, []
        reactions = photo_reactions
        comments = photo_comments
        caption = photo_caption
        ts = photo_ts
        author = photo_author or UserRef()
        final_album = photo_album
        final_url = photo_url_found
        audience = None
        shared_post = None

    post = Post(
        platform="facebook",
        post_id=photo_id,
        url=final_url,
        post_type="album_photo",
        author=author,
        created_at=_ts_to_dt(ts),
        timestamp_unix=ts,
        caption=caption,
        hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(caption))),
        likes=LikesInfo(count=reactions.total or None),
        reactions=reactions,
        comments=comments,
        album_name=final_album,
        shared_post=shared_post,
        audience=audience,
        source="playwright",
    )
    candidates: list[FbMediaDownloadCandidate] = []
    if best_img_uri:
        candidates.append(
            FbMediaDownloadCandidate(
                post_id=photo_id,
                media_id=photo_id,
                media_type="image",
                progressive_url=best_img_uri,
                width=best_img_w,
                height=best_img_h,
            )
        )
    return post, candidates


class FbCollector:
    """Orchestrates Playwright CDP Facebook passes with All-Comments switching and checkpoints."""

    def __init__(
        self,
        page: Any,
        config: FbCollectConfig,
        request_context: Any | None = None,
    ) -> None:
        self.page = page
        self.config = config
        if request_context is not None:
            self.request_context = request_context
        else:
            ctx = getattr(page, "context", None)
            self.request_context = getattr(ctx, "request", None)
        self.collector = ResponseCollector(url_substrings=("/api/graphql/", "/graphql"))
        self.collector.attach(self.page)

    def _safe_body_text(self) -> str:
        try:
            text_attr = getattr(self.page, "inner_text", None)
            if callable(text_attr):
                return text_attr("body") or ""
        except Exception:
            pass
        return ""

    def _visit_and_scroll(self, url: str) -> list[dict[str, Any]]:
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

        last_height = -1
        stagnant = 0
        for _ in range(self.config.max_scrolls):
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", "")), self._safe_body_text())
            for resp in self.collector.drain():
                payloads.extend(resp.payloads)

            try:
                height = self.page.evaluate("() => document.body.scrollHeight")
                self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                break
            self.config.cooldown.wait_step()

            if height == last_height:
                stagnant += 1
                if stagnant >= self.config.max_stagnant_scrolls:
                    break
            else:
                stagnant = 0
                last_height = height

        for resp in self.collector.drain():
            payloads.extend(resp.payloads)
        return payloads

    def run_pass_friends(self) -> FbCollectSummary:
        """Collect Facebook friends list with optional avatars."""
        summary = FbCollectSummary(collect_pass="friends")
        out = self.config.output_dir
        try:
            payloads = self._visit_and_scroll("https://www.facebook.com/me/friends")
            _write_json_atomic(out / "raw" / "friends.json", payloads)
            conns, avatar_urls = extract_fb_friends_from_payloads(payloads)
            if self.config.limit is not None:
                conns = conns[: self.config.limit]

            if self.config.download_avatars and self.request_context is not None:
                for idx, conn in enumerate(conns, start=1):
                    key = conn.user_id or conn.profile_url or conn.full_name or ""
                    avatar_url = avatar_urls.get(key)
                    if avatar_url:
                        fname = f"{_safe_filename_stem(conn.user_id or str(idx))}.jpg"
                        dest = out / "media" / "avatars" / fname
                        try:
                            download_media_via_context(
                                self.request_context,
                                avatar_url,
                                dest,
                                headers={"Referer": "https://www.facebook.com/"},
                            )
                            conn.avatar_local_path = normalize_rel_path(
                                f"media/facebook/avatars/{fname}"
                            )
                            summary.media_saved += 1
                        except Exception:
                            pass

            _write_json_atomic(
                out / "friends.json",
                [c.model_dump(mode="json") for c in conns],
            )
            summary.items_saved = len(conns)
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary

    def _load_owner_profile(self) -> Profile | None:
        cached = getattr(self, "_owner_profile", None)
        if cached is not None:
            return cached
        prof_path = self.config.output_dir / "profile.json"
        if prof_path.exists():
            try:
                cached = Profile.model_validate_json(prof_path.read_text(encoding="utf-8"))
                self._owner_profile = cached
                return cached
            except Exception:
                return None
        return None

    def _load_saved_post(
        self,
        post_id: str,
        saved_posts_mem: dict[str, Post],
    ) -> Post | None:
        prev = saved_posts_mem.get(post_id)
        if prev is not None:
            return prev
        prev_path = self.config.output_dir / "posts" / f"{_safe_filename_stem(post_id)}.json"
        if prev_path.exists():
            try:
                prev = Post.model_validate_json(prev_path.read_text(encoding="utf-8"))
                saved_posts_mem[post_id] = prev
                return prev
            except Exception:
                return None
        return None

    def _persist_post_entries(
        self,
        posts: Sequence[Post],
        candidates: Sequence[FbMediaDownloadCandidate],
        store: CheckpointStore,
        summary: FbCollectSummary,
        saved_posts_mem: dict[str, Post],
        *,
        default_album_name: str | None = None,
        pace_per_item: bool = True,
    ) -> None:
        out = self.config.output_dir
        owner = self._load_owner_profile()
        cand_by_post: dict[str, list[FbMediaDownloadCandidate]] = {}
        for cand in candidates:
            cand_by_post.setdefault(cand.post_id, []).append(cand)

        for post in posts:
            if default_album_name and not post.album_name:
                post = post.model_copy(
                    update={
                        "album_name": default_album_name,
                        "post_type": "album_photo" if post.post_type == "post" else post.post_type,
                    }
                )
            is_owner = _is_fb_owner_post(post, owner)

            store.ensure_items([post.post_id])
            if store.get_status(post.post_id) == "done":
                prev = self._load_saved_post(post.post_id, saved_posts_mem)
                if prev is not None:
                    prev_is_owner = _is_fb_owner_post(prev, owner)
                    merged_comments = (
                        _merge_fb_comments([*prev.comments, *post.comments])
                        if prev_is_owner
                        else []
                    )
                    comments_changed = (
                        len(merged_comments) != len(prev.comments)
                        if not prev_is_owner
                        else len(merged_comments) > len(prev.comments)
                    )
                    best_rx = (
                        prev.reactions
                        if prev.reactions.total >= post.reactions.total
                        else post.reactions
                    )
                    new_media = list(prev.media)
                    media_added = False
                    post_cands = cand_by_post.get(post.post_id, [])
                    regular_cands = [c for c in post_cands if not c.is_link_thumbnail]
                    if not new_media and self.request_context is not None and regular_cands:
                        saved_media = download_post_media(
                            self.request_context,
                            _safe_filename_stem(post.post_id),
                            regular_cands,
                            out / "media" / "posts",
                            created_timestamp=prev.timestamp_unix or post.timestamp_unix,
                            rel_dir="media/facebook/posts",
                        )
                        if saved_media:
                            new_media = saved_media
                            summary.media_saved += len(saved_media)
                            media_added = True

                    merged_album = prev.album_name or post.album_name
                    merged_post_type = (
                        "album_photo"
                        if (merged_album and prev.post_type == "post")
                        else prev.post_type
                    )
                    merged_author = (
                        prev.author
                        if (prev.author.full_name or prev.author.user_id)
                        else post.author
                    )
                    merged_shared = _merge_shared_post_refs(prev.shared_post, post.shared_post)
                    changed = (
                        media_added
                        or comments_changed
                        or best_rx.total > prev.reactions.total
                        or (not prev.album_name and bool(merged_album))
                        or (not prev.caption and bool(post.caption))
                        or (not prev.url and bool(post.url))
                        or (prev.timestamp_unix is None and post.timestamp_unix is not None)
                        or (merged_shared != prev.shared_post)
                    )
                    if changed:
                        updated = prev.model_copy(
                            update={
                                "caption": prev.caption or post.caption,
                                "url": prev.url or post.url,
                                "post_type": merged_post_type,
                                "author": merged_author,
                                "created_at": prev.created_at or post.created_at,
                                "timestamp_unix": prev.timestamp_unix or post.timestamp_unix,
                                "media": new_media,
                                "reactions": best_rx,
                                "likes": LikesInfo(count=best_rx.total or None),
                                "comments": merged_comments,
                                "album_name": merged_album,
                                "shared_post": merged_shared,
                            }
                        )
                        _write_json_atomic(
                            out / "posts" / f"{_safe_filename_stem(post.post_id)}.json",
                            updated.model_dump(mode="json"),
                        )
                        saved_posts_mem[post.post_id] = updated
                continue

            if self.config.limit is not None and summary.items_saved >= self.config.limit:
                break

            if not is_owner and post.comments:
                post = post.model_copy(update={"comments": []})

            store.set_status(post.post_id, "fetched")
            post_cands = cand_by_post.get(post.post_id, [])
            regular_cands = [c for c in post_cands if not c.is_link_thumbnail]
            thumb_cands = [c for c in post_cands if c.is_link_thumbnail and c.progressive_url]
            if self.request_context is not None and regular_cands:
                saved_media = download_post_media(
                    self.request_context,
                    _safe_filename_stem(post.post_id),
                    regular_cands,
                    out / "media" / "posts",
                    created_timestamp=post.timestamp_unix,
                    rel_dir="media/facebook/posts",
                )
                post.media = saved_media
                summary.media_saved += len(saved_media)
            if (
                self.request_context is not None
                and post.shared_post is not None
                and thumb_cands
                and thumb_cands[0].progressive_url
            ):
                stem = _safe_filename_stem(post.post_id)
                thumb_dest = out / "media" / "links" / f"{stem}_thumb.jpg"
                try:
                    download_media_via_context(
                        self.request_context,
                        thumb_cands[0].progressive_url,
                        thumb_dest,
                        headers={"Referer": "https://www.facebook.com/"},
                    )
                    if verify_image(thumb_dest).ok:
                        post.shared_post.thumbnail_local_path = normalize_rel_path(
                            f"media/facebook/links/{stem}_thumb.jpg"
                        )
                        summary.media_saved += 1
                    elif thumb_dest.exists():
                        thumb_dest.unlink()
                except Exception:
                    if thumb_dest.exists():
                        thumb_dest.unlink()
            store.set_status(post.post_id, "media_saved")

            _write_json_atomic(
                out / "posts" / f"{_safe_filename_stem(post.post_id)}.json",
                post.model_dump(mode="json"),
            )
            store.set_status(post.post_id, "done")
            saved_posts_mem[post.post_id] = post
            summary.items_saved += 1
            if pace_per_item:
                self.config.cooldown.record_item_completed()

    def _persist_discovered_posts(
        self,
        payloads: Sequence[dict[str, Any]],
        store: CheckpointStore,
        summary: FbCollectSummary,
        saved_posts_mem: dict[str, Post],
        *,
        save_profile: bool = False,
        default_album_name: str | None = None,
        pace_per_item: bool = True,
        expected_comments_out: dict[str, int] | None = None,
    ) -> int:
        out = self.config.output_dir

        if save_profile and not (out / "profile.json").exists():
            prof, avatar_url = extract_fb_profile_from_payloads(payloads)
            if prof is not None:
                if avatar_url and self.config.download_avatars and self.request_context is not None:
                    avatar_dest = out / "media" / "profile" / "avatar.jpg"
                    try:
                        download_media_via_context(
                            self.request_context,
                            avatar_url,
                            avatar_dest,
                            headers={"Referer": "https://www.facebook.com/"},
                        )
                        prof.avatar_local_path = normalize_rel_path(
                            "media/facebook/profile/avatar.jpg"
                        )
                    except Exception:
                        pass
                _write_json_atomic(out / "profile.json", prof.model_dump(mode="json"))
                self._owner_profile = prof

        posts, candidates = extract_fb_posts_from_payloads(payloads)
        owner = self._load_owner_profile()
        self._persist_post_entries(
            posts,
            candidates,
            store,
            summary,
            saved_posts_mem,
            default_album_name=default_album_name,
            pace_per_item=pace_per_item,
        )

        if expected_comments_out is not None:
            posts_by_id = {p.post_id: p for p in posts}
            batch_counts = extract_fb_comment_counts_from_payloads(payloads)
            for pid, cnt in batch_counts.items():
                p_obj = posts_by_id.get(pid) or self._load_saved_post(pid, saved_posts_mem)
                if p_obj is not None and not _is_fb_owner_post(p_obj, owner):
                    continue
                if cnt > expected_comments_out.get(pid, 0):
                    expected_comments_out[pid] = cnt
                rec = store.get_record(pid)
                if rec is not None and cnt > int(rec.meta.get("expected_comment_count") or 0):
                    store.set_status(
                        pid,
                        rec.status,
                        error=rec.error,
                        meta={"expected_comment_count": cnt},
                    )
        return len(posts)

    def _dismiss_open_dialog(self) -> None:
        try:
            has_dialog = self.page.evaluate(
                "() => document.querySelectorAll('[role=\"dialog\"]').length > 0"
            )
        except Exception:
            has_dialog = False
        if has_dialog is True:
            kb = getattr(self.page, "keyboard", None)
            press = getattr(kb, "press", None)
            if callable(press):
                try:
                    press("Escape")
                except Exception:
                    pass

    def _scroll_and_persist_url(
        self,
        collect_pass: Literal["posts", "albums"],
        url: str,
        store: CheckpointStore,
        summary: FbCollectSummary,
        saved_posts_mem: dict[str, Post],
        batch_counter: list[int],
        *,
        default_album_name: str | None = None,
        expand_comments: bool = True,
        expected_comments_out: dict[str, int] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        out = self.config.output_dir
        self.collector.drain()
        self.page.goto(url, wait_until="domcontentloaded")
        self.config.cooldown.wait_step()
        self.collector.raise_if_unsafe()
        check_page_safety(str(getattr(self.page, "url", url)), self._safe_body_text())

        retained_album_payloads: list[dict[str, Any]] = []
        step_payloads: list[dict[str, Any]] = []
        html = ""
        try:
            raw_html = self.page.content()
            if isinstance(raw_html, str) and raw_html:
                html = raw_html
                step_payloads.extend(extract_ssr_json_payloads(html))
        except Exception:
            pass
        for resp in self.collector.drain():
            step_payloads.extend(resp.payloads)

        def _flush_batch(current_batch: list[dict[str, Any]]) -> tuple[int, bool | None]:
            if expand_comments:
                switch_to_all_comments(self.page, self.config.cooldown)
                expand_fb_comments(
                    self.page,
                    self.config.cooldown,
                    max_clicks=self.config.max_expand_clicks,
                )
                self.collector.raise_if_unsafe()
                for r in self.collector.drain():
                    current_batch.extend(r.payloads)
            if not current_batch:
                return 0, None
            _write_json_atomic(
                out / "raw" / collect_pass / f"batch_{batch_counter[0]:04d}.json",
                current_batch,
            )
            batch_counter[0] += 1
            if collect_pass == "albums":
                retained_album_payloads.extend(current_batch)
            before_saved = summary.items_saved
            stories_seen = self._persist_discovered_posts(
                current_batch,
                store,
                summary,
                saved_posts_mem,
                save_profile=(collect_pass == "posts"),
                default_album_name=default_album_name,
                pace_per_item=expand_comments,
                expected_comments_out=expected_comments_out,
            )
            if not expand_comments and summary.items_saved > before_saved:
                self.config.cooldown.record_item_completed()
            has_next = (
                extract_fb_timeline_has_next_page(current_batch)
                if collect_pass == "posts"
                else None
            )
            return stories_seen, has_next

        _, init_has_next = _flush_batch(step_payloads)
        if init_has_next is False:
            return retained_album_payloads, html

        try:
            last_height = self.page.evaluate("() => document.body.scrollHeight")
            if not isinstance(last_height, (int, float)):
                last_height = -1
        except Exception:
            last_height = -1

        stagnant = 0
        for _ in range(self.config.max_scrolls):
            if self.config.limit is not None and summary.items_saved >= self.config.limit:
                break
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", "")), self._safe_body_text())
            scroll_batch: list[dict[str, Any]] = []
            for resp in self.collector.drain():
                scroll_batch.extend(resp.payloads)

            if not expand_comments:
                self._dismiss_open_dialog()

            try:
                if stagnant > 0 and not expand_comments:
                    self.page.evaluate("() => window.scrollBy(0, -350)")
                self.page.evaluate(
                    "() => { window.scrollTo(0, document.body.scrollHeight); "
                    "if (document.documentElement) document.documentElement.scrollTop = document.documentElement.scrollHeight; }"
                )
            except Exception:
                break
            self.config.cooldown.wait_step()
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", "")), self._safe_body_text())
            for resp in self.collector.drain():
                scroll_batch.extend(resp.payloads)

            try:
                height = self.page.evaluate("() => document.body.scrollHeight")
                if not isinstance(height, (int, float)):
                    height = last_height
            except Exception:
                height = last_height

            stories_seen, has_next = _flush_batch(scroll_batch)
            if has_next is False:
                break

            grew = height > last_height
            if grew or stories_seen > 0:
                stagnant = 0
                if height > last_height:
                    last_height = height
            else:
                stagnant += 1
                if stagnant >= self.config.max_stagnant_scrolls:
                    break

        return retained_album_payloads, html

    def _seed_expected_comments_from_existing(
        self,
        store: CheckpointStore,
    ) -> dict[str, int]:
        expected: dict[str, int] = {}
        for item_id, rec in store._state.items.items():
            cnt = int(rec.meta.get("expected_comment_count") or 0)
            if cnt > 0:
                expected[item_id] = cnt
        raw_posts_dir = self.config.output_dir / "raw" / "posts"
        if raw_posts_dir.exists():
            for bf in sorted(raw_posts_dir.glob("batch_*.json")):
                try:
                    payloads = json.loads(bf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(payloads, list):
                    for pid, cnt in extract_fb_comment_counts_from_payloads(payloads).items():
                        if cnt > expected.get(pid, 0):
                            expected[pid] = cnt
        return expected

    def _collect_comments_for_posts(
        self,
        expected_comments: dict[str, int],
        store: CheckpointStore,
        summary: FbCollectSummary,
        saved_posts_mem: dict[str, Post],
    ) -> None:
        out = self.config.output_dir
        owner = self._load_owner_profile()
        visited = 0
        for post_id, expected_cnt in expected_comments.items():
            if expected_cnt <= 0:
                continue
            if self.config.limit is not None and visited >= self.config.limit:
                break
            rec = store.get_record(post_id)
            if rec is not None and rec.meta.get("comments_fetched"):
                continue

            prev = self._load_saved_post(post_id, saved_posts_mem)
            if prev is None:
                continue
            if not _is_fb_owner_post(prev, owner):
                if prev.comments:
                    cleared = prev.model_copy(update={"comments": []})
                    _write_json_atomic(
                        out / "posts" / f"{_safe_filename_stem(post_id)}.json",
                        cleared.model_dump(mode="json"),
                    )
                    saved_posts_mem[post_id] = cleared
                cur_status = store.get_status(post_id) or "done"
                store.set_status(
                    post_id,
                    cur_status,
                    meta={"comments_fetched": True, "comments_skipped_non_owner": True},
                )
                continue
            if not prev.url:
                continue
            if len(prev.comments) >= expected_cnt:
                if rec is not None:
                    store.set_status(
                        post_id,
                        rec.status,
                        error=rec.error,
                        meta={"comments_fetched": True, "expected_comment_count": expected_cnt},
                    )
                continue

            self.collector.drain()
            self.page.goto(prev.url, wait_until="domcontentloaded")
            self.config.cooldown.wait_step()
            self.collector.raise_if_unsafe()
            check_page_safety(str(getattr(self.page, "url", prev.url)), self._safe_body_text())

            detail_payloads: list[dict[str, Any]] = []
            try:
                raw_html = self.page.content()
                if isinstance(raw_html, str) and raw_html:
                    detail_payloads.extend(extract_ssr_json_payloads(raw_html))
            except Exception:
                pass
            for resp in self.collector.drain():
                detail_payloads.extend(resp.payloads)

            switch_to_all_comments(self.page, self.config.cooldown)
            expand_fb_comments(
                self.page,
                self.config.cooldown,
                max_clicks=self.config.max_expand_clicks,
            )
            self.collector.raise_if_unsafe()
            for resp in self.collector.drain():
                detail_payloads.extend(resp.payloads)

            stem = _safe_filename_stem(post_id)
            if detail_payloads:
                _write_json_atomic(out / "raw" / "posts" / f"comments_{stem}.json", detail_payloads)

            new_comments, new_rx = extract_fb_comments_and_reactions_from_payloads(
                detail_payloads,
                post_id=post_id,
            )
            merged_comments = _merge_fb_comments([*prev.comments, *new_comments])
            best_rx = prev.reactions if prev.reactions.total >= new_rx.total else new_rx
            if len(merged_comments) > len(prev.comments) or best_rx.total > prev.reactions.total:
                updated = prev.model_copy(
                    update={
                        "comments": merged_comments,
                        "reactions": best_rx,
                        "likes": LikesInfo(count=best_rx.total or None),
                    }
                )
                _write_json_atomic(
                    out / "posts" / f"{stem}.json",
                    updated.model_dump(mode="json"),
                )
                saved_posts_mem[post_id] = updated

            cur_status = store.get_status(post_id) or "done"
            store.set_status(
                post_id,
                cur_status,
                meta={"comments_fetched": True, "expected_comment_count": expected_cnt},
            )
            visited += 1
            summary.comments_posts_fetched += 1
            self.config.cooldown.record_item_completed()

    def _visit_and_persist_album_photo(
        self,
        target: FbAlbumPhotoTarget,
        store: CheckpointStore,
        summary: FbCollectSummary,
        saved_posts_mem: dict[str, Post],
        batch_counter: list[int],
    ) -> None:
        out = self.config.output_dir
        self.collector.drain()
        self.page.goto(target.photo_url, wait_until="domcontentloaded")
        self.config.cooldown.wait_step()
        self.collector.raise_if_unsafe()
        check_page_safety(str(getattr(self.page, "url", target.photo_url)), self._safe_body_text())

        photo_payloads: list[dict[str, Any]] = []
        try:
            raw_html = self.page.content()
            if isinstance(raw_html, str) and raw_html:
                photo_payloads.extend(extract_ssr_json_payloads(raw_html))
        except Exception:
            pass
        for resp in self.collector.drain():
            photo_payloads.extend(resp.payloads)

        switch_to_all_comments(self.page, self.config.cooldown)
        expand_fb_comments(
            self.page,
            self.config.cooldown,
            max_clicks=self.config.max_expand_clicks,
        )
        self.collector.raise_if_unsafe()
        for resp in self.collector.drain():
            photo_payloads.extend(resp.payloads)

        if photo_payloads:
            _write_json_atomic(
                out / "raw" / "albums" / f"batch_{batch_counter[0]:04d}.json",
                photo_payloads,
            )
            batch_counter[0] += 1

        post, candidates = extract_fb_photo_post_from_payloads(
            photo_payloads,
            photo_id=target.photo_id,
            photo_url=target.photo_url,
            album_name=target.album_name,
            fallback_image_url=target.fallback_image_url,
        )
        if post is not None:
            self._persist_post_entries(
                [post],
                candidates,
                store,
                summary,
                saved_posts_mem,
                default_album_name=target.album_name,
            )

    def run_pass_posts(self) -> FbCollectSummary:
        """Collect Facebook timeline posts in 2 stages: uninterrupted scroll, then per-post comment expansion."""
        summary = FbCollectSummary(collect_pass="posts")
        store = CheckpointStore(self.config.output_dir / "state_fb.json")
        saved_mem: dict[str, Post] = {}
        try:
            expected_comments = self._seed_expected_comments_from_existing(store)
            self._scroll_and_persist_url(
                "posts",
                "https://www.facebook.com/me",
                store,
                summary,
                saved_mem,
                [1],
                expand_comments=False,
                expected_comments_out=expected_comments,
            )
            self._collect_comments_for_posts(
                expected_comments,
                store,
                summary,
                saved_mem,
            )
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary

    def run_pass_comments(self) -> FbCollectSummary:
        """Run only Stage 2: collect comments/replies on owner posts that expect comments."""
        summary = FbCollectSummary(collect_pass="comments")
        store = CheckpointStore(self.config.output_dir / "state_fb.json")
        saved_mem: dict[str, Post] = {}
        try:
            expected_comments = self._seed_expected_comments_from_existing(store)
            self._collect_comments_for_posts(
                expected_comments,
                store,
                summary,
                saved_mem,
            )
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary

    def run_pass_albums(self) -> FbCollectSummary:
        """Collect Facebook photo albums index, album pages, and individual album photos."""
        summary = FbCollectSummary(collect_pass="albums")
        store = CheckpointStore(self.config.output_dir / "state_fb.json")
        saved_mem: dict[str, Post] = {}
        batch_counter = [1]
        try:
            payloads, html = self._scroll_and_persist_url(
                "albums",
                "https://www.facebook.com/me/photos_albums",
                store,
                summary,
                saved_mem,
                batch_counter,
                expand_comments=False,
            )
            album_urls = extract_fb_album_urls(payloads, html)
            album_titles = extract_fb_album_titles(payloads)
            for alb_url in album_urls:
                if self.config.limit is not None and summary.items_saved >= self.config.limit:
                    break
                alb_title = _lookup_album_title(album_titles, alb_url)
                alb_payloads, _ = self._scroll_and_persist_url(
                    "albums",
                    alb_url,
                    store,
                    summary,
                    saved_mem,
                    batch_counter,
                    default_album_name=alb_title,
                    expand_comments=False,
                )
                if not alb_title:
                    alb_title = _lookup_album_title(extract_fb_album_titles(alb_payloads), alb_url)
                photo_targets = extract_fb_album_photo_targets(
                    alb_payloads,
                    album_url=alb_url,
                    album_name=alb_title,
                )
                for target in photo_targets:
                    if self.config.limit is not None and summary.items_saved >= self.config.limit:
                        break
                    if store.get_status(target.photo_id) == "done":
                        prev = self._load_saved_post(target.photo_id, saved_mem)
                        if prev is not None and prev.media and (
                            not target.album_name or prev.album_name
                        ):
                            continue
                    self._visit_and_persist_album_photo(
                        target,
                        store,
                        summary,
                        saved_mem,
                        batch_counter,
                    )
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary


def collect_facebook(config: FbCollectConfig) -> FbCollectSummary:
    """Attach to running Chrome over CDP and run the selected Facebook collection pass."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        _browser, context = connect_cdp(pw, config.cdp_url)
        page = context.new_page()
        try:
            runner = FbCollector(page, config, request_context=context.request)
            dispatch = {
                "posts": runner.run_pass_posts,
                "comments": runner.run_pass_comments,
                "friends": runner.run_pass_friends,
                "albums": runner.run_pass_albums,
            }
            return dispatch[config.collect_pass]()
        finally:
            page.close()
