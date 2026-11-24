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
    ActivityItem,
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
    Story,
    UserRef,
)

_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")
_PROFILE_ALBUM_NAMES = {"프로필 사진", "profile pictures"}
_KO_GROUP_EXPLICIT_RE = re.compile(r"님이\s+(.+?)\s+그룹(?:에|에서|의)\s+")
_KO_GROUP_FALLBACK_RE = re.compile(
    r"님이\s+(.+?)(?:에|에서)\s+(?:게시물|글|사진|동영상|링크|파일|설문)"
)
_EN_GROUP_RE = re.compile(
    r"(?:posted in|shared (?:a [^\s]+ )?(?:in|to)(?: the group:?)?|uploaded a file in|commented in)\s+(.+?)\.?$",
    re.IGNORECASE,
)
_KO_TARGET_AUTHOR_RE = re.compile(r"님이\s+(.+?)님의\s+")
_EN_TARGET_AUTHOR_RE = re.compile(r"commented on\s+(.+?)'s\s+", re.IGNORECASE)


def _extract_fb_group_name(title: str | None) -> str | None:
    if not isinstance(title, str) or not title.strip():
        return None
    text = title.strip()
    m = _KO_GROUP_EXPLICIT_RE.search(text)
    if m:
        return m.group(1).strip() or None
    m = _KO_GROUP_FALLBACK_RE.search(text)
    if m:
        cand = m.group(1).removesuffix(" 그룹").strip()
        if cand and "타임라인" not in cand:
            return cand
    m = _EN_GROUP_RE.search(text)
    if m:
        return m.group(1).strip() or None
    return None


def _extract_fb_target_author(title: str | None) -> str | None:
    if not isinstance(title, str) or not title.strip():
        return None
    text = title.strip()
    m = _KO_TARGET_AUTHOR_RE.search(text)
    if m:
        cand = m.group(1).strip()
        return cand if cand and cand != "자신" else None
    m = _EN_TARGET_AUTHOR_RE.search(text)
    if m:
        cand = m.group(1).strip()
        return cand if cand and cand.lower() not in {"their own", "his own", "her own"} else None
    return None


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


def _find_fb_profile_album_avatar(export_root: Path) -> str | None:
    album_files: list[Path] = []
    for rel_dir in ("your_facebook_activity/posts/album", "photos_and_videos/album"):
        d = export_root.joinpath(*rel_dir.split("/"))
        if d.exists():
            album_files.extend(sorted(d.glob("*.json")))

    for af in album_files:
        data = load_fixed_json(af)
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if not isinstance(name, str) or name.strip().lower() not in _PROFILE_ALBUM_NAMES:
            continue
        cover = data.get("cover_photo")
        if isinstance(cover, dict):
            uri = normalize_uri_if_local(cover.get("uri"))
            if uri:
                return uri
        photos = data.get("photos")
        if isinstance(photos, list):
            for ph in reversed(photos):
                if isinstance(ph, dict):
                    uri = normalize_uri_if_local(ph.get("uri"))
                    if uri:
                        return uri
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
    if avatar_uri is None:
        avatar_uri = _find_fb_profile_album_avatar(export_root)

    if not any((full_name, username, bio, avatar_uri)):
        return None
    return Profile(
        platform="facebook",
        username=username,
        full_name=full_name,
        bio=bio,
        avatar_local_path=avatar_uri,
    )


def _extract_fb_entry_attachments(
    entry: dict[str, Any],
) -> tuple[list[MediaItem], SharedPostRef | None, Location | None, str | None]:
    media_items: list[MediaItem] = []
    shared_post: SharedPostRef | None = None
    location: Location | None = None
    life_event_title: str | None = None

    for att in entry.get("attachments", []) if isinstance(entry.get("attachments"), list) else []:
        if not isinstance(att, dict):
            continue
        for adata in att.get("data", []) if isinstance(att.get("data"), list) else []:
            if not isinstance(adata, dict):
                continue
            if isinstance(adata.get("media"), dict) and adata["media"].get("uri"):
                media_items.append(media_item_from_meta_entry(adata["media"]))
            elif isinstance(adata.get("file"), dict) and adata["file"].get("uri"):
                media_items.append(media_item_from_meta_entry(adata["file"]))
            elif isinstance(adata.get("external_context"), dict) and shared_post is None:
                ext = adata["external_context"]
                clean_url = clean_fb_external_url(
                    ext.get("url") if isinstance(ext.get("url"), str) else None
                )
                title = ext.get("name") if isinstance(ext.get("name"), str) and ext["name"].strip() else None
                desc = ext.get("description") if isinstance(ext.get("description"), str) and ext["description"].strip() else None
                src_name = ext.get("source") if isinstance(ext.get("source"), str) and ext["source"].strip() else None
                dom = extract_domain(clean_url) or src_name
                if any((clean_url, title, desc, dom)):
                    shared_post = SharedPostRef(
                        url=clean_url,
                        title=title,
                        text=desc,
                        domain=dom,
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
            elif isinstance(adata.get("life_event"), dict) and life_event_title is None:
                le = adata["life_event"]
                if isinstance(le.get("title"), str) and le["title"].strip():
                    life_event_title = le["title"].strip()

    return media_items, shared_post, location, life_event_title


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
                if not isinstance(d_item, dict):
                    continue
                if not caption and isinstance(d_item.get("post"), str):
                    caption = d_item["post"]
                if ts is None:
                    ts = safe_int(d_item.get("backdated_timestamp")) or safe_int(
                        d_item.get("update_timestamp")
                    )

            raw_title = entry.get("title")
            activity_title = (
                raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
            )

            tagged_users: list[UserRef] = []
            for t_entry in entry.get("tags", []) if isinstance(entry.get("tags"), list) else []:
                if isinstance(t_entry, dict) and isinstance(t_entry.get("name"), str):
                    t_name = t_entry["name"].strip()
                    if t_name:
                        tagged_users.append(UserRef(full_name=t_name))

            media_items, shared_post, location, life_event_title = _extract_fb_entry_attachments(entry)
            if not caption and life_event_title:
                caption = life_event_title

            is_share_title = bool(
                activity_title
                and ("공유" in activity_title or "shared" in activity_title.lower())
            )
            post_type = (
                "shared"
                if (shared_post is not None or is_share_title) and not media_items
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
                    activity_title=activity_title,
                    hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(caption))),
                    tagged_users=tagged_users,
                    location=location,
                    media=media_items,
                    shared_post=shared_post,
                    source="official_export",
                )
            )

    # Deduplicate identical stub entries that Facebook repeats across/within your_posts_*.json
    deduped: list[Post] = []
    seen_keys: set[tuple[Any, ...]] = set()
    for p in posts:
        dedup_key = (
            p.timestamp_unix,
            " ".join(p.caption.split()),
            p.activity_title or "",
            tuple(m.local_path or m.original_uri or "" for m in p.media),
            p.shared_post.url if p.shared_post else None,
            p.shared_post.title if p.shared_post else None,
        )
        if p.timestamp_unix is not None and dedup_key in seen_keys:
            continue
        if p.timestamp_unix is not None:
            seen_keys.add(dedup_key)
        deduped.append(p)
    return deduped


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
        photos = data.get("photos") if isinstance(data.get("photos"), list) else []

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
                new_p = Post(
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
                extra_posts.append(new_p)
                if item.local_path:
                    uri_to_post[item.local_path] = new_p

        cover = data.get("cover_photo")
        if isinstance(cover, dict) and cover.get("uri"):
            cover_item = media_item_from_meta_entry(cover)
            if cover_item.local_path and cover_item.local_path not in uri_to_post:
                cts = safe_int(cover.get("creation_timestamp")) or safe_int(
                    data.get("last_modified_timestamp")
                )
                cdesc = cover.get("description") if isinstance(cover.get("description"), str) else ""
                cover_p = Post(
                    platform="facebook",
                    post_id=f"fb_album_cover_{cts or 0}_{cover_item.media_id or af.stem}",
                    post_type="album_photo",
                    created_at=ts_to_dt(cts),
                    timestamp_unix=cts,
                    caption=cdesc,
                    hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(cdesc))),
                    media=[cover_item],
                    album_name=album_name,
                    source="official_export",
                )
                extra_posts.append(cover_p)
                uri_to_post[cover_item.local_path] = cover_p

    return [*posts, *extra_posts]


def _parse_fb_group_posts(export_root: Path) -> list[Post]:
    grp_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/groups/group_posts_and_comments.json",
            "groups/group_posts_and_comments.json",
            "groups/your_posts_and_comments_in_groups.json",
        ),
    )
    if grp_path is None:
        return []
    data = load_fixed_json(grp_path)
    raw_list: Any = data
    if isinstance(data, dict):
        raw_list = data.get("group_posts_v2")
        if isinstance(raw_list, dict):
            raw_list = next((v for v in raw_list.values() if isinstance(v, list)), [])
    if not isinstance(raw_list, list):
        return []

    group_posts: list[Post] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            continue
        ts = safe_int(entry.get("timestamp"))
        caption = ""
        explicit_group: str | None = None
        for d_item in entry.get("data", []) if isinstance(entry.get("data"), list) else []:
            if not isinstance(d_item, dict):
                continue
            if not caption and isinstance(d_item.get("post"), str):
                caption = d_item["post"]
            if not explicit_group and isinstance(d_item.get("group"), str):
                explicit_group = d_item["group"].strip() or None

        raw_title = entry.get("title")
        activity_title = (
            raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
        )
        group_name = explicit_group or _extract_fb_group_name(activity_title)

        media_items, shared_post, location, life_event_title = _extract_fb_entry_attachments(entry)
        if not caption and life_event_title:
            caption = life_event_title

        first_id = media_items[0].media_id if media_items and media_items[0].media_id else str(idx)
        group_posts.append(
            Post(
                platform="facebook",
                post_id=f"fb_group_{ts or 0}_{first_id}",
                post_type="group_post",
                created_at=ts_to_dt(ts),
                timestamp_unix=ts,
                caption=caption,
                activity_title=activity_title,
                group_name=group_name,
                hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(caption))),
                location=location,
                media=media_items,
                shared_post=shared_post,
                source="official_export",
            )
        )
    return group_posts


def _merge_fb_uncategorized_photos(export_root: Path, posts: list[Post]) -> list[Post]:
    photo_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/posts/your_uncategorized_photos.json",
            "photos_and_videos/your_uncategorized_photos.json",
        ),
    )
    if photo_path is None:
        return posts
    data = load_fixed_json(photo_path)
    raw_list = data.get("other_photos_v2") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return posts

    uri_to_post: dict[str, Post] = {
        m.local_path: p for p in posts for m in p.media if m.local_path
    }
    extra_posts: list[Post] = []
    for idx, ph in enumerate(raw_list):
        if not isinstance(ph, dict) or not ph.get("uri"):
            continue
        item = media_item_from_meta_entry(ph)
        ts = safe_int(ph.get("creation_timestamp"))
        desc = ph.get("description") if isinstance(ph.get("description"), str) else ""
        matched = uri_to_post.get(item.local_path) if item.local_path else None
        if matched is not None:
            if not matched.caption and desc:
                matched.caption = desc
        else:
            extra_posts.append(
                Post(
                    platform="facebook",
                    post_id=f"fb_photo_{ts or 0}_{item.media_id or idx}",
                    post_type="album_photo",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=desc,
                    hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(desc))),
                    media=[item],
                    album_name="미분류 사진",
                    source="official_export",
                )
            )
    return [*posts, *extra_posts]


def _merge_fb_videos(export_root: Path, posts: list[Post]) -> list[Post]:
    vid_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/posts/your_videos.json",
            "photos_and_videos/your_videos.json",
        ),
    )
    if vid_path is None:
        return posts
    data = load_fixed_json(vid_path)
    raw_list = data.get("videos_v2") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return posts

    uri_to_post: dict[str, Post] = {
        m.local_path: p for p in posts for m in p.media if m.local_path
    }
    extra_posts: list[Post] = []
    for idx, vid in enumerate(raw_list):
        if not isinstance(vid, dict) or not vid.get("uri"):
            continue
        item = media_item_from_meta_entry(vid)
        if item.media_type == "unknown":
            item.media_type = "video"
        ts = safe_int(vid.get("creation_timestamp"))
        desc = vid.get("description") if isinstance(vid.get("description"), str) else ""
        matched = uri_to_post.get(item.local_path) if item.local_path else None
        if matched is not None:
            if not matched.caption and desc:
                matched.caption = desc
        else:
            extra_posts.append(
                Post(
                    platform="facebook",
                    post_id=f"fb_video_{ts or 0}_{item.media_id or idx}",
                    post_type="post",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=desc,
                    hashtags=list(dict.fromkeys(_HASHTAG_RE.findall(desc))),
                    media=[item],
                    source="official_export",
                )
            )
    return [*posts, *extra_posts]


def _parse_fb_stories(export_root: Path) -> list[Story]:
    stories_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/stories/archived_stories.json",
            "stories/archived_stories.json",
        ),
    )
    if stories_path is None:
        return []
    data = load_fixed_json(stories_path)
    raw_list = data.get("archived_stories_v2") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return []

    stories: list[Story] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            continue
        ts = safe_int(entry.get("timestamp"))
        story_caption = ""
        for d_item in entry.get("data", []) if isinstance(entry.get("data"), list) else []:
            if isinstance(d_item, dict) and isinstance(d_item.get("post"), str) and d_item["post"].strip():
                story_caption = d_item["post"].strip()
                break
        if not story_caption and isinstance(entry.get("title"), str) and entry["title"].strip():
            story_caption = entry["title"].strip()

        media_items: list[MediaItem] = []
        for att in entry.get("attachments", []) if isinstance(entry.get("attachments"), list) else []:
            if not isinstance(att, dict):
                continue
            for adata in att.get("data", []) if isinstance(att.get("data"), list) else []:
                if isinstance(adata, dict) and isinstance(adata.get("media"), dict) and adata["media"].get("uri"):
                    media_items.append(media_item_from_meta_entry(adata["media"]))
        if not media_items:
            stories.append(
                Story(
                    platform="facebook",
                    story_id=f"fb_story_{ts or 0}_{idx}",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=story_caption,
                    source="official_export",
                    media=None,
                )
            )
            continue
        for m_idx, item in enumerate(media_items):
            suffix = f"{item.media_id or idx}" if len(media_items) == 1 else f"{item.media_id or idx}_{m_idx}"
            stories.append(
                Story(
                    platform="facebook",
                    story_id=f"fb_story_{ts or 0}_{suffix}",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=story_caption,
                    source="official_export",
                    media=item,
                )
            )
    return stories


def _parse_fb_friends(export_root: Path) -> list[Connection]:
    connections: list[Connection] = []

    friends_path = _first_existing(
        export_root,
        (
            "connections/friends/your_friends.json",
            "friends_and_followers/friends.json",
        ),
    )
    if friends_path is not None:
        data = load_fixed_json(friends_path)
        raw_list = data.get("friends_v2") if isinstance(data, dict) else data
        if isinstance(raw_list, list):
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

    following_path = _first_existing(
        export_root,
        (
            "connections/followers/who_you've_followed.json",
            "friends_and_followers/who_you've_followed.json",
            "following_and_followers/following.json",
        ),
    )
    if following_path is not None:
        fdata = load_fixed_json(following_path)
        f_list: Any = fdata
        if isinstance(fdata, dict):
            f_list = fdata.get("following_v3")
            if not isinstance(f_list, list):
                f_list = fdata.get("following_v2")
        if isinstance(f_list, list):
            for entry in f_list:
                if not isinstance(entry, dict):
                    continue
                f_name = entry.get("name")
                if not f_name and isinstance(entry.get("data"), list) and entry["data"]:
                    first_d = entry["data"][0]
                    if isinstance(first_d, dict):
                        f_name = first_d.get("name")
                if not f_name:
                    continue
                ts = safe_int(entry.get("timestamp"))
                connections.append(
                    Connection(
                        platform="facebook",
                        relation="following",
                        full_name=str(f_name),
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
    avatar_local_path: str | None = None
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
        if avatar_local_path is None and isinstance(data.get("image"), dict):
            avatar_local_path = normalize_uri_if_local(data["image"].get("uri"))
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
            for mkey in ("photos", "videos", "audio_files", "gifs", "files"):
                for m_entry in raw_m.get(mkey, []) if isinstance(raw_m.get(mkey), list) else []:
                    if isinstance(m_entry, dict) and m_entry.get("uri"):
                        item = media_item_from_meta_entry(m_entry)
                        if mkey == "audio_files":
                            item.media_type = "audio"
                        elif mkey in ("photos", "gifs") and item.media_type == "unknown":
                            item.media_type = "image"
                        elif mkey == "videos" and item.media_type == "unknown":
                            item.media_type = "video"
                        media_items.append(item)
            sticker = raw_m.get("sticker")
            if isinstance(sticker, dict) and sticker.get("uri"):
                s_item = media_item_from_meta_entry(sticker)
                if s_item.media_type == "unknown":
                    s_item.media_type = "image"
                media_items.append(s_item)
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
            share_link = (
                share.get("link")
                if isinstance(share, dict) and isinstance(share.get("link"), str)
                else None
            )
            share_text = (
                share.get("share_text")
                if isinstance(share, dict) and isinstance(share.get("share_text"), str)
                else None
            )
            share_owner = (
                share.get("original_content_owner")
                if isinstance(share, dict) and isinstance(share.get("original_content_owner"), str)
                else None
            )
            is_unsent = bool(raw_m.get("is_unsent") or raw_m.get("isUnsent"))
            is_missed_call = bool(raw_m.get("missed"))

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
                    share_text=share_text,
                    share_owner=share_owner,
                    is_unsent=is_unsent,
                    is_missed_call=is_missed_call,
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
        avatar_local_path=avatar_local_path,
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
        for bucket in ("inbox", "e2ee_cutover", "archived_threads", "message_requests", "filtered_threads"):
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
                    force_pending=(bucket in ("message_requests", "filtered_threads")),
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


def _parse_fb_activities(export_root: Path) -> list[ActivityItem]:
    activities: list[ActivityItem] = []

    comments_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/comments_and_reactions/comments.json",
            "comments_and_reactions/comments.json",
            "comments/comments.json",
        ),
    )
    if comments_path is not None:
        data = load_fixed_json(comments_path)
        raw_list = data.get("comments_v2") if isinstance(data, dict) else data
        if isinstance(raw_list, list):
            for idx, entry in enumerate(raw_list):
                if not isinstance(entry, dict):
                    continue
                ts = safe_int(entry.get("timestamp"))
                title = (
                    entry.get("title").strip()
                    if isinstance(entry.get("title"), str) and entry.get("title").strip()
                    else None
                )
                c_text = ""
                for d_item in entry.get("data", []) if isinstance(entry.get("data"), list) else []:
                    if isinstance(d_item, dict) and isinstance(d_item.get("comment"), dict):
                        c_obj = d_item["comment"]
                        if isinstance(c_obj.get("comment"), str) and not c_text:
                            c_text = c_obj["comment"]
                        if ts is None:
                            ts = safe_int(c_obj.get("timestamp"))
                media_items, shared_post, _, _ = _extract_fb_entry_attachments(entry)
                for m in media_items:
                    if m.media_type == "unknown" and m.local_path and "stickers_used/" in m.local_path:
                        m.media_type = "image"
                activities.append(
                    ActivityItem(
                        activity_id=f"fb_comment_{ts or 0}_{idx}",
                        platform="facebook",
                        kind="own_comment",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        text=c_text,
                        title=title,
                        target_author=_extract_fb_target_author(title),
                        url=shared_post.url if shared_post else None,
                        media=media_items,
                        source="official_export",
                    )
                )

    grp_comments_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/groups/your_comments_in_groups.json",
            "groups/your_comments_in_groups.json",
        ),
    )
    if grp_comments_path is not None:
        data = load_fixed_json(grp_comments_path)
        raw_list = data.get("group_comments_v2") if isinstance(data, dict) else data
        if isinstance(raw_list, list):
            for idx, entry in enumerate(raw_list):
                if not isinstance(entry, dict):
                    continue
                ts = safe_int(entry.get("timestamp"))
                title = (
                    entry.get("title").strip()
                    if isinstance(entry.get("title"), str) and entry.get("title").strip()
                    else None
                )
                c_text = ""
                group_name: str | None = None
                for d_item in entry.get("data", []) if isinstance(entry.get("data"), list) else []:
                    if isinstance(d_item, dict) and isinstance(d_item.get("comment"), dict):
                        c_obj = d_item["comment"]
                        if isinstance(c_obj.get("comment"), str) and not c_text:
                            c_text = c_obj["comment"]
                        if isinstance(c_obj.get("group"), str) and c_obj["group"].strip() and not group_name:
                            group_name = c_obj["group"].strip()
                        if ts is None:
                            ts = safe_int(c_obj.get("timestamp"))
                if not group_name:
                    group_name = _extract_fb_group_name(title)
                media_items, shared_post, _, _ = _extract_fb_entry_attachments(entry)
                activities.append(
                    ActivityItem(
                        activity_id=f"fb_grp_comment_{ts or 0}_{idx}",
                        platform="facebook",
                        kind="group_comment",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        text=c_text,
                        title=title,
                        target_author=_extract_fb_target_author(title),
                        group_name=group_name,
                        url=shared_post.url if shared_post else None,
                        media=media_items,
                        source="official_export",
                    )
                )

    saved_path = _first_existing(
        export_root,
        (
            "your_facebook_activity/saved_items_and_collections/your_saved_items.json",
            "saved_items_and_collections/your_saved_items.json",
        ),
    )
    if saved_path is not None:
        data = load_fixed_json(saved_path)
        raw_list = data.get("saves_v2") if isinstance(data, dict) else data
        if isinstance(raw_list, list):
            for idx, entry in enumerate(raw_list):
                if not isinstance(entry, dict):
                    continue
                ts = safe_int(entry.get("timestamp"))
                title = (
                    entry.get("title").strip()
                    if isinstance(entry.get("title"), str) and entry.get("title").strip()
                    else None
                )
                media_items, shared_post, _, _ = _extract_fb_entry_attachments(entry)
                text = (shared_post.title or shared_post.text or "") if shared_post else ""
                activities.append(
                    ActivityItem(
                        activity_id=f"fb_saved_{ts or 0}_{idx}",
                        platform="facebook",
                        kind="saved_item",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        text=text,
                        title=title,
                        target_author=shared_post.domain if shared_post else None,
                        url=shared_post.url if shared_post else None,
                        media=media_items,
                        source="official_export",
                    )
                )

    return activities


def parse_fb_official_export(
    export_root: Path,
    e2ee_root: Path | None = None,
) -> OfficialExportBundle:
    """Parse an extracted Facebook official JSON export and optional E2EE Messenger export."""
    posts = _merge_fb_albums(export_root, _parse_fb_posts(export_root))
    posts = _merge_fb_uncategorized_photos(export_root, posts)
    posts = _merge_fb_videos(export_root, posts)
    posts = [*posts, *_parse_fb_group_posts(export_root)]
    extra_roots = [e2ee_root] if e2ee_root is not None and e2ee_root.exists() else []
    return OfficialExportBundle(
        platform="facebook",
        root_dir=export_root,
        profile=_parse_fb_profile(export_root),
        posts=posts,
        stories=_parse_fb_stories(export_root),
        connections=_parse_fb_friends(export_root),
        messages=_parse_fb_messages(export_root, e2ee_root),
        activities=_parse_fb_activities(export_root),
        extra_roots=extra_roots,
    )
