"""Parser for official Facebook JSON exports and Messenger E2EE Security Storage ZIPs."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any

from meta_archive_pipeline.official import (
    OfficialExportBundle,
    load_fixed_json,
    media_item_from_meta_entry,
    normalize_uri_if_local,
    safe_float,
    safe_int,
    ts_to_dt,
)
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    Location,
    MediaItem,
    Message,
    MessageReaction,
    MessageThread,
    Post,
    Profile,
    SharedPostRef,
    UserRef,
)

_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")


def clean_fb_external_url(raw_url: str | None) -> str | None:
    """Unwrap Facebook `l.facebook.com/l.php?u=...` redirect links and strip empty values."""
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    parsed = urllib.parse.urlparse(cleaned)
    host = parsed.netloc.lower()
    if host.endswith("facebook.com") and parsed.path == "/l.php":
        qs = urllib.parse.parse_qs(parsed.query)
        target = qs.get("u", [None])[0]
        if isinstance(target, str) and target.strip():
            return target.strip()
    return cleaned


def extract_domain(url: str | None) -> str | None:
    """Return normalized lowercase hostname (without leading `www.`) from a URL."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urllib.parse.urlparse(url.strip())
    host = parsed.netloc.lower()
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _first_existing(root: Path, candidates: tuple[str, ...]) -> Path | None:
    for rel in candidates:
        p = root.joinpath(*rel.split("/"))
        if p.exists():
            return p
    return None


def _parse_fb_profile(export_root: Path) -> Profile | None:
    info_path = _first_existing(
        export_root,
        (
            "personal_information/profile_information/profile_information.json",
            "profile_information/profile_information.json",
        ),
    )
    if info_path is None:
        return None
    data = load_fixed_json(info_path)
    pv2 = data.get("profile_v2") if isinstance(data, dict) else data
    if not isinstance(pv2, dict):
        return None

    name_obj = pv2.get("name")
    full_name = name_obj.get("full_name") if isinstance(name_obj, dict) else None
    username = pv2.get("username") if isinstance(pv2.get("username"), str) else None
    bio = pv2.get("intro_bio") if isinstance(pv2.get("intro_bio"), str) else None
    avatar_uri: str | None = None
    pic = pv2.get("profile_pic")
    if isinstance(pic, dict):
        avatar_uri = normalize_uri_if_local(pic.get("uri"))

    if not any((full_name, username, bio, avatar_uri)):
        return None
    return Profile(
        platform="facebook",
        username=username,
        full_name=full_name,
        bio=bio,
        avatar_local_path=avatar_uri,
    )


def _parse_fb_posts(export_root: Path) -> list[Post]:
    post_files: list[Path] = []
    for rel_dir in ("your_facebook_activity/posts", "posts"):
        d = export_root.joinpath(*rel_dir.split("/"))
        if d.exists():
            post_files.extend(sorted(d.glob("your_posts_*.json")))

    posts: list[Post] = []
    seen_files: set[Path] = set()
    for pf in post_files:
        if pf in seen_files:
            continue
        seen_files.add(pf)
        data = load_fixed_json(pf)
        raw_list = data if isinstance(data, list) else data.get("status_updates_v2", [])
        if not isinstance(raw_list, list):
            continue

        for idx, entry in enumerate(raw_list):
            if not isinstance(entry, dict):
                continue
            ts = safe_int(entry.get("timestamp"))
            caption = ""
            for d_item in entry.get("data", []) if isinstance(entry.get("data"), list) else []:
                if isinstance(d_item, dict) and isinstance(d_item.get("post"), str):
                    caption = d_item["post"]
                    break
            if not caption and isinstance(entry.get("title"), str):
                caption = entry["title"]

            media_items: list[MediaItem] = []
            shared_post: SharedPostRef | None = None
            location: Location | None = None

            for att in entry.get("attachments", []) if isinstance(entry.get("attachments"), list) else []:
                if not isinstance(att, dict):
                    continue
                for adata in att.get("data", []) if isinstance(att.get("data"), list) else []:
                    if not isinstance(adata, dict):
                        continue
                    if isinstance(adata.get("media"), dict) and adata["media"].get("uri"):
                        media_items.append(media_item_from_meta_entry(adata["media"]))
                    elif isinstance(adata.get("external_context"), dict) and shared_post is None:
                        ext = adata["external_context"]
                        clean_url = clean_fb_external_url(
                            ext.get("url") if isinstance(ext.get("url"), str) else None
                        )
                        desc = ext.get("description") if isinstance(ext.get("description"), str) else None
                        src_name = ext.get("source") if isinstance(ext.get("source"), str) else None
                        shared_post = SharedPostRef(
                            url=clean_url,
                            title=ext.get("name") if isinstance(ext.get("name"), str) else None,
                            text=desc,
                            domain=extract_domain(clean_url) or src_name,
                        )
                    elif isinstance(adata.get("place"), dict) and location is None:
                        pl = adata["place"]
                        coord = pl.get("coordinate") if isinstance(pl.get("coordinate"), dict) else {}
                        location = Location(
                            name=pl.get("name") if isinstance(pl.get("name"), str) else None,
                            address=pl.get("address") if isinstance(pl.get("address"), str) else None,
                            lat=safe_float(coord.get("latitude")),
                            lng=safe_float(coord.get("longitude")),
                        )

            post_type = (
                "shared"
                if shared_post is not None and not media_items
                else ("carousel" if len(media_items) > 1 else "post")
            )
            first_id = media_items[0].media_id if media_items and media_items[0].media_id else str(idx)
            posts.append(
                Post(
                    platform="facebook",
                    post_id=f"fb_official_{ts or 0}_{first_id}",
                    post_type=post_type,  # type: ignore[arg-type]
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=caption,
                    hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(caption))),
                    location=location,
                    media=media_items,
                    shared_post=shared_post,
                    source="official_export",
                )
            )
    return posts


def _merge_fb_albums(export_root: Path, posts: list[Post]) -> list[Post]:
    album_files: list[Path] = []
    for rel_dir in ("your_facebook_activity/posts/album", "photos_and_videos/album"):
        d = export_root.joinpath(*rel_dir.split("/"))
        if d.exists():
            album_files.extend(sorted(d.glob("*.json")))

    uri_to_post: dict[str, Post] = {}
    for post in posts:
        for media in post.media:
            if media.local_path:
                uri_to_post[media.local_path] = post

    extra_posts: list[Post] = []
    for af in album_files:
        data = load_fixed_json(af)
        if not isinstance(data, dict):
            continue
        album_name = data.get("name") if isinstance(data.get("name"), str) else None
        photos = data.get("photos")
        if not isinstance(photos, list):
            continue

        for idx, ph in enumerate(photos):
            if not isinstance(ph, dict) or not ph.get("uri"):
                continue
            item = media_item_from_meta_entry(ph)
            ts = safe_int(ph.get("creation_timestamp"))
            desc = ph.get("description") if isinstance(ph.get("description"), str) else ""

            comments: list[Comment] = []
            for raw_c in ph.get("comments", []) if isinstance(ph.get("comments"), list) else []:
                if isinstance(raw_c, dict) and raw_c.get("comment"):
                    cts = safe_int(raw_c.get("timestamp"))
                    comments.append(
                        Comment(
                            author=UserRef(
                                full_name=str(raw_c.get("author")) if raw_c.get("author") else None
                            ),
                            text=str(raw_c["comment"]),
                            created_at=ts_to_dt(cts),
                            timestamp_unix=cts,
                        )
                    )

            matched = uri_to_post.get(item.local_path) if item.local_path else None
            if matched is not None:
                if album_name:
                    matched.album_name = matched.album_name or album_name
                    if matched.post_type == "post":
                        matched.post_type = "album_photo"
                if not matched.caption and desc:
                    matched.caption = desc
                if comments and not matched.comments:
                    matched.comments = comments
            else:
                extra_posts.append(
                    Post(
                        platform="facebook",
                        post_id=f"fb_album_{ts or 0}_{item.media_id or idx}",
                        post_type="album_photo",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        caption=desc,
                        hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(desc))),
                        media=[item],
                        comments=comments,
                        album_name=album_name,
                        source="official_export",
                    )
                )

    return [*posts, *extra_posts]


def _parse_fb_friends(export_root: Path) -> list[Connection]:
    friends_path = _first_existing(
        export_root,
        (
            "connections/friends/your_friends.json",
            "friends_and_followers/friends.json",
        ),
    )
    if friends_path is None:
        return []
    data = load_fixed_json(friends_path)
    raw_list = data.get("friends_v2") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return []

    connections: list[Connection] = []
    for entry in raw_list:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        ts = safe_int(entry.get("timestamp"))
        connections.append(
            Connection(
                platform="facebook",
                relation="friend",
                full_name=str(entry["name"]),
                connected_at=ts_to_dt(ts),
                timestamp_unix=ts,
                match_confidence="heuristic",
            )
        )
    return connections


def _parse_meta_or_e2ee_thread_files(
    files: list[Path],
    thread_id: str,
    *,
    force_e2ee: bool,
    force_pending: bool,
) -> MessageThread | None:
    title: str | None = None
    participants: dict[str, UserRef] = {}
    messages: list[Message] = []
    is_e2ee = force_e2ee
    is_pending = force_pending
    has_messages = False

    for fpath in sorted(files):
        data = load_fixed_json(fpath)
        if not isinstance(data, dict) or "messages" not in data:
            continue
        has_messages = True
        if not title:
            raw_title = data.get("title") or data.get("threadName")
            if isinstance(raw_title, str):
                title = raw_title
        if data.get("is_e2ee") is True:
            is_e2ee = True
        if data.get("is_pending") is True:
            is_pending = True

        for p in data.get("participants", []) if isinstance(data.get("participants"), list) else []:
            if isinstance(p, dict) and isinstance(p.get("name"), str):
                participants.setdefault(p["name"], UserRef(full_name=p["name"]))
            elif isinstance(p, str) and p:
                participants.setdefault(p, UserRef(full_name=p))

        for raw_m in data.get("messages", []):
            if not isinstance(raw_m, dict):
                continue
            sender = str(raw_m.get("sender_name") or raw_m.get("senderName") or "")
            ts_ms = safe_int(raw_m.get("timestamp_ms") or raw_m.get("timestamp"))
            if ts_ms is not None and ts_ms < 10_000_000_000:
                ts_sec = ts_ms
                ts_ms_norm = ts_ms * 1000
            else:
                ts_ms_norm = ts_ms
                ts_sec = ts_ms // 1000 if ts_ms is not None else None

            text_val = raw_m.get("content") if "content" in raw_m else raw_m.get("text")
            text = text_val if isinstance(text_val, str) else None

            media_items: list[MediaItem] = []
            for mkey in ("photos", "videos", "audio_files"):
                for m_entry in raw_m.get(mkey, []) if isinstance(raw_m.get(mkey), list) else []:
                    if isinstance(m_entry, dict) and m_entry.get("uri"):
                        item = media_item_from_meta_entry(m_entry)
                        if mkey == "audio_files" and item.media_type == "unknown":
                            item.media_type = "audio"
                        media_items.append(item)
            for m_entry in raw_m.get("media", []) if isinstance(raw_m.get("media"), list) else []:
                if isinstance(m_entry, dict) and m_entry.get("uri"):
                    media_items.append(media_item_from_meta_entry(m_entry))

            reactions: list[MessageReaction] = []
            for rx in raw_m.get("reactions", []) if isinstance(raw_m.get("reactions"), list) else []:
                if isinstance(rx, dict) and rx.get("actor") and rx.get("reaction"):
                    reactions.append(
                        MessageReaction(actor=str(rx["actor"]), reaction=str(rx["reaction"]))
                    )

            share = raw_m.get("share")
            share_link = share.get("link") if isinstance(share, dict) and isinstance(share.get("link"), str) else None
            is_unsent = bool(raw_m.get("is_unsent") or raw_m.get("isUnsent"))

            messages.append(
                Message(
                    sender=sender,
                    timestamp_unix=ts_sec,
                    timestamp_ms=ts_ms_norm,
                    created_at=ts_to_dt(ts_sec),
                    text=text,
                    media=media_items,
                    reactions=reactions,
                    shared_link=share_link,
                    is_unsent=is_unsent,
                    call_duration=safe_int(raw_m.get("call_duration")),
                )
            )

    if not has_messages:
        return None
    messages.sort(key=lambda m: m.timestamp_ms if m.timestamp_ms is not None else 0)
    return MessageThread(
        platform="facebook",
        thread_id=thread_id,
        title=title,
        participants=list(participants.values()),
        is_e2ee=is_e2ee,
        is_pending=is_pending,
        messages=messages,
    )


def _parse_fb_messages(export_root: Path, e2ee_root: Path | None) -> list[MessageThread]:
    threads: list[MessageThread] = []

    msg_roots = [
        p
        for p in (
            export_root / "your_facebook_activity" / "messages",
            export_root / "messages",
        )
        if p.exists()
    ]

    for msg_root in msg_roots:
        for bucket in ("inbox", "e2ee_cutover", "archived_threads", "message_requests"):
            bucket_dir = msg_root / bucket
            if not bucket_dir.exists():
                continue
            by_dir: dict[Path, list[Path]] = {}
            for jf in sorted(bucket_dir.rglob("*.json")):
                by_dir.setdefault(jf.parent, []).append(jf)
            for thread_dir, files in sorted(by_dir.items()):
                rel_id = thread_dir.relative_to(msg_root).as_posix()
                thread = _parse_meta_or_e2ee_thread_files(
                    files,
                    rel_id,
                    force_e2ee=(bucket == "e2ee_cutover"),
                    force_pending=(bucket == "message_requests"),
                )
                if thread is not None:
                    threads.append(thread)

    if e2ee_root is not None and e2ee_root.exists():
        by_dir_e2ee: dict[Path, list[Path]] = {}
        for jf in sorted(e2ee_root.rglob("*.json")):
            by_dir_e2ee.setdefault(jf.parent, []).append(jf)
        for thread_dir, files in sorted(by_dir_e2ee.items()):
            rel_part = thread_dir.relative_to(e2ee_root).as_posix()
            tid = f"e2ee_external/{rel_part if rel_part != '.' else thread_dir.name}"
            thread = _parse_meta_or_e2ee_thread_files(
                files,
                tid,
                force_e2ee=True,
                force_pending=False,
            )
            if thread is not None:
                threads.append(thread)

    return threads


def parse_fb_official_export(
    export_root: Path,
    e2ee_root: Path | None = None,
) -> OfficialExportBundle:
    """Parse an extracted Facebook official JSON export and optional E2EE Messenger export."""
    posts = _merge_fb_albums(export_root, _parse_fb_posts(export_root))
    extra_roots = [e2ee_root] if e2ee_root is not None and e2ee_root.exists() else []
    return OfficialExportBundle(
        platform="facebook",
        root_dir=export_root,
        profile=_parse_fb_profile(export_root),
        posts=posts,
        connections=_parse_fb_friends(export_root),
        messages=_parse_fb_messages(export_root, e2ee_root),
        extra_roots=extra_roots,
    )
