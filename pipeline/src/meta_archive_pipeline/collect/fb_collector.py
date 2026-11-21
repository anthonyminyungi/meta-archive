"""Facebook CDP collector and pure Comet GraphQL/SSR payload parsers."""

from __future__ import annotations

import json
import os
import re
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
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    MediaItem,
    Post,
    ReactionSummary,
    SharedPostRef,
    UserRef,
)
from meta_archive_pipeline.schema.paths import normalize_rel_path
from meta_archive_pipeline.utils.checkpoint import CheckpointStore
from meta_archive_pipeline.utils.fs import atomic_write_bytes

FbPass = Literal["posts", "friends", "albums"]

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
    "답글 보기",
    "View more comments",
    "View previous comments",
    "View more replies",
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


@dataclass
class FbCollectConfig:
    """Configuration for a Facebook CDP collection pass."""

    output_dir: Path
    cdp_url: str = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}"
    collect_pass: FbPass = "posts"
    limit: int | None = None
    download_avatars: bool = True
    cooldown: CooldownTracker = field(default_factory=CooldownTracker)
    max_scrolls: int = 200
    max_expand_clicks: int = 20


@dataclass
class FbCollectSummary:
    """Result metrics for a Facebook collection pass."""

    collect_pass: FbPass
    items_saved: int = 0
    media_saved: int = 0
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


def _safe_filename_stem(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "item"


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

        uid = str(inner.get("id") or "") or None
        full_name: str | None = None
        if isinstance(inner.get("name"), str) and inner["name"]:
            full_name = inner["name"]
        elif isinstance(inner.get("title"), dict) and isinstance(inner["title"].get("text"), str):
            full_name = inner["title"]["text"]

        profile_url = inner.get("url") if isinstance(inner.get("url"), str) else None
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
        for d in _walk_dicts(payload):
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
    for src in (
        (comment_feedback.get("display_comments") or {}).get("edges")
        if isinstance(comment_feedback.get("display_comments"), dict)
        else None,
        (node.get("replies") or {}).get("edges")
        if isinstance(node.get("replies"), dict)
        else None,
    ):
        if isinstance(src, list):
            for edge in src:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    raw_replies.append(edge["node"])
    if isinstance(node.get("child_comments"), list):
        raw_replies.extend(c for c in node["child_comments"] if isinstance(c, dict))

    replies = _merge_fb_comments([_parse_fb_comment_node(r) for r in raw_replies])
    return Comment(
        comment_id=cid,
        author=author,
        text=text,
        created_at=_ts_to_dt(ts),
        timestamp_unix=ts,
        like_count=reactions.total or None,
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
    cri = feedback.get("comment_rendering_instance")
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


def _find_story_feedback(story: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(story.get("feedback"), dict):
        return story["feedback"]
    comet = story.get("comet_sections")
    if isinstance(comet, dict):
        fb_sec = comet.get("feedback")
        if isinstance(fb_sec, dict):
            fb_story = fb_sec.get("story")
            if isinstance(fb_story, dict):
                if isinstance(fb_story.get("feedback"), dict):
                    return fb_story["feedback"]
                fc = fb_story.get("feedback_context")
                if isinstance(fc, dict):
                    ft = fc.get("feedback_target_with_context")
                    if isinstance(ft, dict):
                        if isinstance(ft.get("ufi_renderer"), dict) and isinstance(
                            ft["ufi_renderer"].get("feedback"), dict
                        ):
                            return ft["ufi_renderer"]["feedback"]
                        return ft
    return None


def _is_top_fb_story(d: dict[str, Any]) -> bool:
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


def extract_fb_posts_from_payloads(
    payloads: Sequence[dict[str, Any]],
) -> tuple[list[Post], list[FbMediaDownloadCandidate]]:
    """Extract normalized Facebook Posts and media download candidates from GraphQL/SSR payloads."""
    posts_by_id: dict[str, Post] = {}
    candidates_by_post: dict[str, list[FbMediaDownloadCandidate]] = {}

    for payload in payloads:
        for d in _walk_dicts(payload):
            if not _is_top_fb_story(d):
                continue

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

            raw_attached = d.get("attached_story") or content_story.get("attached_story")
            shared_post: SharedPostRef | None = None
            if isinstance(raw_attached, dict):
                att_actors = raw_attached.get("actors") or []
                att_author = (
                    att_actors[0].get("name")
                    if isinstance(att_actors, list) and att_actors and isinstance(att_actors[0], dict)
                    else None
                )
                att_msg = raw_attached.get("message")
                att_text = (
                    att_msg.get("text")
                    if isinstance(att_msg, dict) and isinstance(att_msg.get("text"), str)
                    else None
                )
                shared_post = SharedPostRef(
                    url=raw_attached.get("permalink_url") or raw_attached.get("url"),
                    author_name=att_author,
                    text=att_text,
                    title=raw_attached.get("title") if isinstance(raw_attached.get("title"), str) else None,
                )

            feedback = _find_story_feedback(d)
            reactions = _extract_fb_reactions(feedback)
            comments = _merge_fb_comments(_extract_comments_from_feedback(feedback))

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
                    sub_nodes: list[dict[str, Any]] = []
                    for sub_key in ("all_subattachments", "subattachments"):
                        sub = att.get(sub_key)
                        if isinstance(sub, dict) and isinstance(sub.get("nodes"), list):
                            sub_nodes.extend(n for n in sub["nodes"] if isinstance(n, dict))

                    if sub_nodes:
                        for sub_node in sub_nodes:
                            m = sub_node.get("media") if isinstance(sub_node.get("media"), dict) else sub_node
                            if isinstance(m, dict):
                                if not album_name and isinstance(m.get("album"), dict):
                                    album_name = m["album"].get("name")
                                _extract_fb_media_from_attachment(post_id, m, post_candidates)
                    elif isinstance(att.get("media"), dict):
                        m = att["media"]
                        if not album_name and isinstance(m.get("album"), dict):
                            album_name = m["album"].get("name")
                        _extract_fb_media_from_attachment(post_id, m, post_candidates)

            if album_name:
                post_type: Literal["post", "carousel", "reel", "album_photo", "shared", "other"] = "album_photo"
            elif shared_post is not None and not post_candidates:
                post_type = "shared"
            elif len(post_candidates) > 1:
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
                        "shared_post": prev.shared_post or new_post.shared_post,
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


def switch_to_all_comments(page: Any, cooldown: CooldownTracker) -> int:
    """Switch Facebook comment filter from 'Most relevant' to 'All comments'."""
    switched = 0
    while _click_first_visible(page, _RELEVANT_SORT_LABELS, ("button",)):
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


def expand_fb_comments(
    page: Any,
    cooldown: CooldownTracker,
    max_clicks: int = 20,
) -> int:
    """Click visible Facebook expand/reply comment buttons."""
    clicks = 0
    while clicks < max_clicks:
        if _click_first_visible(page, _FB_EXPAND_LABELS, ("button",)):
            clicks += 1
            cooldown.wait_step()
        else:
            break
    return clicks


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
                if stagnant >= 2:
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
                from meta_archive_pipeline.utils.media import download_media_via_context

                for idx, conn in enumerate(conns, start=1):
                    key = conn.user_id or conn.profile_url or conn.full_name or ""
                    avatar_url = avatar_urls.get(key)
                    if avatar_url:
                        fname = f"{_safe_filename_stem(conn.user_id or str(idx))}.jpg"
                        dest = out / "media" / "avatars" / fname
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

            _write_json_atomic(
                out / "friends.json",
                [c.model_dump(mode="json") for c in conns],
            )
            summary.items_saved = len(conns)
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary

    def _run_timeline_or_albums(self, collect_pass: Literal["posts", "albums"], url: str) -> FbCollectSummary:
        summary = FbCollectSummary(collect_pass=collect_pass)
        out = self.config.output_dir
        try:
            payloads = self._visit_and_scroll(url)
            switch_to_all_comments(self.page, self.config.cooldown)
            expand_fb_comments(
                self.page,
                self.config.cooldown,
                max_clicks=self.config.max_expand_clicks,
            )
            self.collector.raise_if_unsafe()
            for resp in self.collector.drain():
                payloads.extend(resp.payloads)

            _write_json_atomic(out / "raw" / collect_pass / "batch_0001.json", payloads)
            posts, candidates = extract_fb_posts_from_payloads(payloads)
            if self.config.limit is not None:
                posts = posts[: self.config.limit]
                kept_ids = {p.post_id for p in posts}
                candidates = [c for c in candidates if c.post_id in kept_ids]

            cand_by_post: dict[str, list[FbMediaDownloadCandidate]] = {}
            for cand in candidates:
                cand_by_post.setdefault(cand.post_id, []).append(cand)

            store = CheckpointStore(out / "state_fb.json")
            store.ensure_items([p.post_id for p in posts])

            for post in posts:
                if store.get_status(post.post_id) == "done":
                    continue
                store.set_status(post.post_id, "fetched")
                post_cands = cand_by_post.get(post.post_id, [])
                if self.request_context is not None and post_cands:
                    saved_media = download_post_media(
                        self.request_context,
                        _safe_filename_stem(post.post_id),
                        post_cands,
                        out / "media" / "posts",
                        created_timestamp=post.timestamp_unix,
                        rel_dir="media/facebook/posts",
                    )
                    post.media = saved_media
                    summary.media_saved += len(saved_media)
                store.set_status(post.post_id, "media_saved")

                _write_json_atomic(
                    out / "posts" / f"{_safe_filename_stem(post.post_id)}.json",
                    post.model_dump(mode="json"),
                )
                store.set_status(post.post_id, "done")
                summary.items_saved += 1
                self.config.cooldown.record_item_completed()
        except SafetyStopError as exc:
            summary.paused_reason = str(exc)
        return summary

    def run_pass_posts(self) -> FbCollectSummary:
        """Collect Facebook timeline posts with All-Comments switching and media download."""
        return self._run_timeline_or_albums("posts", "https://www.facebook.com/me")

    def run_pass_albums(self) -> FbCollectSummary:
        """Collect Facebook photo albums."""
        return self._run_timeline_or_albums("albums", "https://www.facebook.com/me/photos_albums")


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
                "friends": runner.run_pass_friends,
                "albums": runner.run_pass_albums,
            }
            return dispatch[config.collect_pass]()
        finally:
            page.close()
