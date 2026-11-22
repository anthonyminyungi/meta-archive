"""Parser for official Instagram JSON exports (including 2026 label_values schema)."""

from __future__ import annotations

import re
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
    Connection,
    Location,
    MediaItem,
    Message,
    MessageReaction,
    MessageThread,
    Post,
    Profile,
    Story,
    UserRef,
)

_HASHTAG_RE = re.compile(r"#([\w\uac00-\ud7a3]+)")


def _parse_ig_profile(export_root: Path) -> Profile | None:
    info_path = export_root / "personal_information" / "personal_information" / "personal_information.json"
    username: str | None = None
    full_name: str | None = None
    bio: str | None = None
    avatar_local_path: str | None = None

    if info_path.exists():
        data = load_fixed_json(info_path)
        users = data.get("profile_user") if isinstance(data, dict) else None
        if isinstance(users, list) and users and isinstance(users[0], dict):
            user_obj = users[0]
            smap = user_obj.get("string_map_data") or {}
            if isinstance(smap, dict):
                for key in ("Username", "사용자 이름"):
                    if isinstance(smap.get(key), dict) and smap[key].get("value"):
                        username = str(smap[key]["value"])
                        break
                for key in ("Name", "이름"):
                    if isinstance(smap.get(key), dict) and smap[key].get("value"):
                        full_name = str(smap[key]["value"])
                        break
                for key in ("Bio", "소개"):
                    if isinstance(smap.get(key), dict) and smap[key].get("value"):
                        bio = str(smap[key]["value"])
                        break
            mmap = user_obj.get("media_map_data") or {}
            if isinstance(mmap, dict):
                for key in ("Profile Photo", "프로필 사진"):
                    if isinstance(mmap.get(key), dict) and mmap[key].get("uri"):
                        avatar_local_path = normalize_uri_if_local(mmap[key]["uri"])
                        break

    if not avatar_local_path:
        pic_path = export_root / "your_instagram_activity" / "media" / "profile_photos.json"
        if pic_path.exists():
            pic_data = load_fixed_json(pic_path)
            pics = pic_data.get("ig_profile_picture") if isinstance(pic_data, dict) else None
            if isinstance(pics, list) and pics and isinstance(pics[0], dict):
                avatar_local_path = normalize_uri_if_local(pics[0].get("uri"))

    if not any((username, full_name, bio, avatar_local_path)):
        return None
    return Profile(
        platform="instagram",
        username=username,
        full_name=full_name,
        bio=bio,
        avatar_local_path=avatar_local_path,
    )


def _parse_legacy_post_list(
    entries: list[Any],
    *,
    is_archived: bool = False,
    default_type: str = "post",
    prefix: str = "ig",
) -> list[Post]:
    posts: list[Post] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        raw_media = entry.get("media")
        if isinstance(raw_media, list):
            media_dicts = [m for m in raw_media if isinstance(m, dict)]
        elif "uri" in entry:
            media_dicts = [entry]
        else:
            media_dicts = []

        media_items = [media_item_from_meta_entry(m) for m in media_dicts]
        caption = entry.get("title") if isinstance(entry.get("title"), str) else ""
        if not caption and media_dicts and isinstance(media_dicts[0].get("title"), str):
            caption = media_dicts[0]["title"]

        ts = safe_int(entry.get("creation_timestamp"))
        if ts is None and media_dicts:
            ts = safe_int(media_dicts[0].get("creation_timestamp"))

        if default_type == "reel":
            post_type = "reel"
        elif len(media_items) > 1:
            post_type = "carousel"
        else:
            post_type = "post"

        first_id = (
            media_items[0].media_id
            if media_items and media_items[0].media_id
            else f"{idx}"
        )
        post_id = f"{prefix}_{ts or 0}_{first_id}"
        hashtags = list(dict.fromkeys(_HASHTAG_RE.findall(caption)))

        posts.append(
            Post(
                platform="instagram",
                post_id=post_id,
                post_type=post_type,  # type: ignore[arg-type]
                created_at=ts_to_dt(ts),
                timestamp_unix=ts,
                caption=caption,
                hashtags=hashtags,
                media=media_items,
                is_archived=is_archived,
                source="official_export",
            )
        )
    return posts


def _parse_bool_str(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return None


def _unwrap_lv_records(raw_dict: Any) -> list[list[dict[str, Any]]]:
    """Normalize both flat `[{'label': ...}]` and 2026 wrapped `[{'title': '', 'dict': [...]}]` lists."""
    if not isinstance(raw_dict, list) or not raw_dict:
        return []
    groups: list[list[dict[str, Any]]] = []
    flat_buf: list[dict[str, Any]] = []
    for elem in raw_dict:
        if isinstance(elem, list):
            if flat_buf:
                groups.append(flat_buf)
                flat_buf = []
            groups.append([x for x in elem if isinstance(x, dict)])
        elif isinstance(elem, dict):
            if "label" not in elem and isinstance(elem.get("dict"), list):
                if flat_buf:
                    groups.append(flat_buf)
                    flat_buf = []
                groups.append([x for x in elem["dict"] if isinstance(x, dict)])
            else:
                flat_buf.append(elem)
    if flat_buf:
        groups.append(flat_buf)
    return groups


def _extract_metadata_dims(meta_dict_list: Any) -> tuple[float | None, int | None, int | None]:
    """Extract `(duration_sec, width, height)` from a `label='Metadata'` sub-dict list."""
    dur: float | None = None
    w: int | None = None
    h: int | None = None
    for group in _unwrap_lv_records(meta_dict_list):
        for sub in group:
            lbl = (sub.get("label") or "").strip().lower()
            if lbl == "duration":
                dv = safe_float(sub.get("value"))
                if dv is not None and dv > 0:
                    dur = dv
            elif lbl == "width":
                wv = safe_int(sub.get("value"))
                if wv and wv > 0:
                    w = wv
            elif lbl == "height":
                hv = safe_int(sub.get("value"))
                if hv and hv > 0:
                    h = hv
    return dur, w, h


def _extract_label_dict_fields(lv_list: Any) -> dict[str, Any]:
    """Flatten one level of label_values entries into structured fields."""
    fields: dict[str, Any] = {
        "media_uris": [],
        "caption": None,
        "creation_time": None,
        "lat": None,
        "lng": None,
        "is_audio_muted": None,
        "width": None,
        "height": None,
        "duration_sec": None,
        "is_reel": False,
        "hashtags": [],
        "place": None,
        "children": [],
    }
    if not isinstance(lv_list, list):
        return fields

    for item in lv_list:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if title == "Media" and isinstance(item.get("dict"), list):
            for group in _unwrap_lv_records(item["dict"]):
                fields["children"].append(_extract_label_dict_fields(group))
            continue

        if title == "Place" and isinstance(item.get("dict"), list):
            pname = None
            pdesc = None
            for group in _unwrap_lv_records(item["dict"]):
                for sub in group:
                    if sub.get("label") == "Name" and sub.get("value"):
                        pname = str(sub["value"])
                    elif sub.get("label") == "Description" and sub.get("value"):
                        pdesc = str(sub["value"])
            if pname or pdesc:
                fields["place"] = Location(name=pname, description=pdesc)
            continue

        if title == "Hashtags" and isinstance(item.get("dict"), list):
            for group in _unwrap_lv_records(item["dict"]):
                for sub in group:
                    if sub.get("label") == "Name" and sub.get("value"):
                        tag = str(sub["value"]).lstrip("#").strip()
                        if tag:
                            fields["hashtags"].append(tag)
            continue

        if title == "Reel metadata" and isinstance(item.get("dict"), list) and item["dict"]:
            fields["is_reel"] = True
            continue

        label = item.get("label")
        if label == "Media" and isinstance(item.get("media"), list):
            for m in item["media"]:
                if isinstance(m, dict) and m.get("uri"):
                    fields["media_uris"].append(m["uri"])
        elif label in ("Caption", "Title") and item.get("value") and not fields["caption"]:
            fields["caption"] = str(item["value"])
        elif label == "Creation time" and item.get("timestamp_value"):
            fields["creation_time"] = safe_int(item["timestamp_value"])
        elif label == "Latitude":
            lat_v = safe_float(item.get("value"))
            if lat_v is not None and lat_v != 0.0:
                fields["lat"] = lat_v
        elif label == "Longitude":
            lng_v = safe_float(item.get("value"))
            if lng_v is not None and lng_v != 0.0:
                fields["lng"] = lng_v
        elif label == "Is audio muted":
            muted = _parse_bool_str(item.get("value"))
            if muted is not None:
                fields["is_audio_muted"] = muted
        elif label == "Original width":
            w = safe_int(item.get("value"))
            if w and w > 0:
                fields["width"] = w
        elif label == "Original height":
            h = safe_int(item.get("value"))
            if h and h > 0:
                fields["height"] = h
        elif label == "Metadata" and isinstance(item.get("dict"), list):
            dur, mw, mh = _extract_metadata_dims(item["dict"])
            if dur is not None:
                fields["duration_sec"] = dur
            if mw and not fields["width"]:
                fields["width"] = mw
            if mh and not fields["height"]:
                fields["height"] = mh

        # Some 2026 exports nest multiple slide Media groups inside item["dict"] sequentially
        if isinstance(item.get("dict"), list) and not title and label != "Metadata":
            sub_parsed = _extract_label_dict_fields(item["dict"])
            if sub_parsed["media_uris"]:
                fields["children"].append(sub_parsed)

    return fields


def _collect_slide_media_from_nested(lv_list: Any) -> list[dict[str, Any]]:
    """Split nested or wrapped Media sub-entries into per-slide media metadata dicts."""
    if not isinstance(lv_list, list):
        return []
    slides: list[dict[str, Any]] = []
    for item in lv_list:
        if not isinstance(item, dict):
            continue
        if item.get("title") == "Media" and isinstance(item.get("dict"), list):
            for group in _unwrap_lv_records(item["dict"]):
                current: dict[str, Any] = {}
                for sub in group:
                    lbl = sub.get("label")
                    if lbl == "Media" and isinstance(sub.get("media"), list):
                        if "uri" in current:
                            slides.append(current)
                            current = {}
                        for m in sub["media"]:
                            if isinstance(m, dict) and m.get("uri"):
                                current["uri"] = m["uri"]
                                break
                    elif lbl == "Is audio muted":
                        muted = _parse_bool_str(sub.get("value"))
                        if muted is not None:
                            current["is_audio_muted"] = muted
                    elif lbl == "Original width":
                        wv = safe_int(sub.get("value"))
                        if wv and wv > 0:
                            current["width"] = wv
                    elif lbl == "Original height":
                        hv = safe_int(sub.get("value"))
                        if hv and hv > 0:
                            current["height"] = hv
                    elif lbl == "Metadata" and isinstance(sub.get("dict"), list):
                        dur, mw, mh = _extract_metadata_dims(sub["dict"])
                        if dur is not None:
                            current["duration_sec"] = dur
                        if mw and not current.get("width"):
                            current["width"] = mw
                        if mh and not current.get("height"):
                            current["height"] = mh
                    elif lbl == "Latitude":
                        lat_v = safe_float(sub.get("value"))
                        if lat_v is not None and lat_v != 0.0:
                            current["lat"] = lat_v
                    elif lbl == "Longitude":
                        lng_v = safe_float(sub.get("value"))
                        if lng_v is not None and lng_v != 0.0:
                            current["lng"] = lng_v
                if "uri" in current:
                    slides.append(current)
    return slides


def _merge_posts_json_enrichment(posts: list[Post], posts_json_path: Path) -> list[Post]:
    if not posts_json_path.exists():
        return posts
    raw_items = load_fixed_json(posts_json_path)
    if not isinstance(raw_items, list):
        return posts

    uri_to_post: dict[str, Post] = {}
    for post in posts:
        for media in post.media:
            if media.local_path:
                uri_to_post[media.local_path] = post

    extra_posts: list[Post] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        lvs = item.get("label_values")
        parsed = _extract_label_dict_fields(lvs)
        slides = _collect_slide_media_from_nested(lvs)

        top_uris = [u for u in (normalize_uri_if_local(u) for u in parsed["media_uris"]) if u]
        slide_uris = [u for u in (normalize_uri_if_local(s.get("uri")) for s in slides) if u]
        all_uris = list(dict.fromkeys([*top_uris, *slide_uris]))

        matched_post: Post | None = None
        for uri in all_uris:
            if uri in uri_to_post:
                matched_post = uri_to_post[uri]
                break

        slide_by_uri: dict[str, dict[str, Any]] = {}
        for s in slides:
            norm = normalize_uri_if_local(s.get("uri"))
            if norm:
                slide_by_uri[norm] = s

        loc: Location | None = parsed["place"]
        if parsed["lat"] is not None or parsed["lng"] is not None:
            if loc is None:
                loc = Location(lat=parsed["lat"], lng=parsed["lng"])
            else:
                loc.lat = loc.lat if loc.lat is not None else parsed["lat"]
                loc.lng = loc.lng if loc.lng is not None else parsed["lng"]

        if matched_post is not None:
            if not matched_post.caption and parsed["caption"]:
                matched_post.caption = parsed["caption"]
            if parsed["hashtags"]:
                matched_post.hashtags = list(
                    dict.fromkeys([*matched_post.hashtags, *parsed["hashtags"]])
                )
            if loc is not None:
                if matched_post.location is None:
                    matched_post.location = loc
                else:
                    matched_post.location.name = matched_post.location.name or loc.name
                    matched_post.location.description = (
                        matched_post.location.description or loc.description
                    )
                    matched_post.location.lat = matched_post.location.lat or loc.lat
                    matched_post.location.lng = matched_post.location.lng or loc.lng

            if parsed["is_reel"] and matched_post.post_type == "post" and len(matched_post.media) == 1:
                if matched_post.media[0].media_type == "video":
                    matched_post.post_type = "reel"

            existing_paths = {m.local_path for m in matched_post.media if m.local_path}
            for media in matched_post.media:
                if media.local_path and media.local_path in slide_by_uri:
                    smeta = slide_by_uri[media.local_path]
                    if media.is_audio_muted is None and smeta.get("is_audio_muted") is not None:
                        media.is_audio_muted = smeta["is_audio_muted"]
                    if media.width is None and smeta.get("width"):
                        media.width = smeta["width"]
                    if media.height is None and smeta.get("height"):
                        media.height = smeta["height"]
                    if media.duration_sec is None and smeta.get("duration_sec") is not None:
                        media.duration_sec = smeta["duration_sec"]
                    if media.gps_lat is None and smeta.get("lat") is not None:
                        media.gps_lat = smeta["lat"]
                    if media.gps_lng is None and smeta.get("lng") is not None:
                        media.gps_lng = smeta["lng"]
                elif len(matched_post.media) == 1:
                    if media.is_audio_muted is None and parsed["is_audio_muted"] is not None:
                        media.is_audio_muted = parsed["is_audio_muted"]
                    if media.width is None and parsed["width"]:
                        media.width = parsed["width"]
                    if media.height is None and parsed["height"]:
                        media.height = parsed["height"]
                    if media.duration_sec is None and parsed["duration_sec"] is not None:
                        media.duration_sec = parsed["duration_sec"]

            # Add any extra top-level carousel cover URI missing from posts_1.json slides
            for cover_uri in top_uris:
                if cover_uri not in existing_paths:
                    cover_item = media_item_from_meta_entry({"uri": cover_uri})
                    cover_item.is_carousel_cover = True
                    cover_item.width = cover_item.width or parsed["width"]
                    cover_item.height = cover_item.height or parsed["height"]
                    if cover_item.duration_sec is None:
                        cover_item.duration_sec = parsed["duration_sec"]
                    if cover_item.is_audio_muted is None:
                        cover_item.is_audio_muted = parsed["is_audio_muted"]
                    matched_post.media.insert(0, cover_item)
                    uri_to_post[cover_uri] = matched_post
                    existing_paths.add(cover_uri)
        else:
            ts = parsed["creation_time"] or safe_int(item.get("timestamp"))
            caption = parsed["caption"] or ""
            hashtags = list(
                dict.fromkeys([*parsed["hashtags"], *_HASHTAG_RE.findall(caption)])
            )
            media_items: list[MediaItem] = []
            for u_idx, uri in enumerate(all_uris):
                mi = media_item_from_meta_entry({"uri": uri})
                if len(all_uris) > 1 and u_idx == 0 and uri in top_uris and uri not in slide_uris:
                    mi.is_carousel_cover = True
                smeta = slide_by_uri.get(uri)
                if smeta:
                    mi.is_audio_muted = smeta.get("is_audio_muted")
                    mi.width = smeta.get("width")
                    mi.height = smeta.get("height")
                    mi.duration_sec = smeta.get("duration_sec")
                elif u_idx == 0:
                    mi.is_audio_muted = parsed["is_audio_muted"]
                    mi.width = parsed["width"]
                    mi.height = parsed["height"]
                    mi.duration_sec = parsed["duration_sec"]
                media_items.append(mi)

            first_id = media_items[0].media_id if media_items and media_items[0].media_id else str(idx)
            extra_posts.append(
                Post(
                    platform="instagram",
                    post_id=f"ig_postsjson_{ts or 0}_{first_id}",
                    post_type="carousel" if len(media_items) > 1 else "post",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    caption=caption,
                    hashtags=hashtags,
                    location=loc,
                    media=media_items,
                    source="official_export",
                )
            )

    return [*posts, *extra_posts]


def _parse_ig_stories(export_root: Path, warnings: list[str]) -> list[Story]:
    stories_path = export_root / "your_instagram_activity" / "media" / "stories.json"
    if not stories_path.exists():
        return []
    data = load_fixed_json(stories_path)
    raw_list = data.get("ig_stories") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return []

    stories: list[Story] = []
    for idx, entry in enumerate(raw_list):
        if not isinstance(entry, dict):
            continue
        ts = safe_int(entry.get("creation_timestamp"))
        caption = entry.get("title") if isinstance(entry.get("title"), str) else ""
        raw_uri = entry.get("uri") if isinstance(entry.get("uri"), str) else ""
        if not raw_uri.strip():
            warnings.append(f"stories.json entry #{idx} has empty URI (timestamp={ts})")
            media = None
            sid = f"ig_story_{ts or 0}_{idx}"
        else:
            media = media_item_from_meta_entry(entry)
            sid = f"ig_story_{ts or 0}_{media.media_id or idx}"

        stories.append(
            Story(
                platform="instagram",
                story_id=sid,
                created_at=ts_to_dt(ts),
                timestamp_unix=ts,
                caption=caption,
                source="official_export",
                media=media,
            )
        )
    return stories


def _parse_ig_connections(export_root: Path) -> list[Connection]:
    conn_dir = export_root / "connections" / "followers_and_following"
    if not conn_dir.exists():
        return []

    connections: list[Connection] = []

    for fpath in sorted(conn_dir.glob("followers_*.json")):
        data = load_fixed_json(fpath)
        entries = data if isinstance(data, list) else data.get("relationships_followers", [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sdata = entry.get("string_list_data") or []
            first = sdata[0] if isinstance(sdata, list) and sdata and isinstance(sdata[0], dict) else {}
            username = first.get("value") or entry.get("title") or None
            href = first.get("href") or (f"https://www.instagram.com/{username}/" if username else None)
            ts = safe_int(first.get("timestamp"))
            if username:
                connections.append(
                    Connection(
                        platform="instagram",
                        relation="follower",
                        username=str(username),
                        profile_url=href,
                        connected_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        match_confidence="exact",
                    )
                )

    following_path = conn_dir / "following.json"
    if following_path.exists():
        data = load_fixed_json(following_path)
        entries = data.get("relationships_following") if isinstance(data, dict) else data
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sdata = entry.get("string_list_data") or []
                first = sdata[0] if isinstance(sdata, list) and sdata and isinstance(sdata[0], dict) else {}
                # 2026 following.json puts username in entry["title"] without "value"
                username = first.get("value") or entry.get("title") or None
                href = first.get("href") or (
                    f"https://www.instagram.com/{username}/" if username else None
                )
                ts = safe_int(first.get("timestamp"))
                if username:
                    connections.append(
                        Connection(
                            platform="instagram",
                            relation="following",
                            username=str(username),
                            profile_url=href,
                            connected_at=ts_to_dt(ts),
                            timestamp_unix=ts,
                            match_confidence="exact",
                        )
                    )

    return connections


def _parse_ig_messages(export_root: Path) -> list[MessageThread]:
    msg_root = export_root / "your_instagram_activity" / "messages"
    if not msg_root.exists():
        return []

    threads_by_dir: dict[Path, list[Path]] = {}
    for bucket in ("inbox", "message_requests", "filtered_threads"):
        bucket_dir = msg_root / bucket
        if not bucket_dir.exists():
            continue
        for json_file in sorted(bucket_dir.rglob("*.json")):
            threads_by_dir.setdefault(json_file.parent, []).append(json_file)

    threads: list[MessageThread] = []
    for thread_dir, files in sorted(threads_by_dir.items()):
        rel_dir = thread_dir.relative_to(msg_root).as_posix()
        is_pending = rel_dir.startswith("message_requests/")
        title: str | None = None
        avatar_local_path: str | None = None
        participants: dict[str, UserRef] = {}
        messages: list[Message] = []

        for fpath in sorted(files):
            data = load_fixed_json(fpath)
            if not isinstance(data, dict) or "messages" not in data:
                continue
            if not title and isinstance(data.get("title"), str):
                title = data["title"]
            if not avatar_local_path and isinstance(data.get("image"), dict):
                avatar_local_path = normalize_uri_if_local(data["image"].get("uri"))
            if data.get("is_pending") is True:
                is_pending = True
            for p in data.get("participants", []) if isinstance(data.get("participants"), list) else []:
                if isinstance(p, dict) and isinstance(p.get("name"), str):
                    pname = p["name"]
                    participants.setdefault(pname, UserRef(full_name=pname))

            for raw_m in data.get("messages", []):
                if not isinstance(raw_m, dict):
                    continue
                sender = str(raw_m.get("sender_name") or "")
                ts_ms = safe_int(raw_m.get("timestamp_ms"))
                ts_sec = ts_ms // 1000 if ts_ms is not None else None
                text = raw_m.get("content") if isinstance(raw_m.get("content"), str) else None

                media_items: list[MediaItem] = []
                for mkey in ("photos", "videos", "audio_files"):
                    for m_entry in raw_m.get(mkey, []) if isinstance(raw_m.get(mkey), list) else []:
                        if isinstance(m_entry, dict) and m_entry.get("uri"):
                            item = media_item_from_meta_entry(m_entry)
                            if mkey == "audio_files":
                                item.media_type = "audio"
                            elif mkey == "photos" and item.media_type == "unknown":
                                item.media_type = "image"
                            elif mkey == "videos" and item.media_type == "unknown":
                                item.media_type = "video"
                            media_items.append(item)

                reactions: list[MessageReaction] = []
                for rx in raw_m.get("reactions", []) if isinstance(raw_m.get("reactions"), list) else []:
                    if isinstance(rx, dict) and rx.get("actor") and rx.get("reaction"):
                        reactions.append(
                            MessageReaction(actor=str(rx["actor"]), reaction=str(rx["reaction"]))
                        )

                share_link: str | None = None
                share_text: str | None = None
                share_owner: str | None = None
                share = raw_m.get("share")
                if isinstance(share, dict):
                    if isinstance(share.get("link"), str) and share["link"].strip():
                        share_link = share["link"].strip()
                    if isinstance(share.get("share_text"), str) and share["share_text"].strip():
                        share_text = share["share_text"].strip()
                    owner_cand = (
                        share.get("original_content_owner")
                        or share.get("profile_share_username")
                        or share.get("profile_share_name")
                    )
                    if isinstance(owner_cand, str) and owner_cand.strip():
                        share_owner = owner_cand.strip()

                messages.append(
                    Message(
                        sender=sender,
                        timestamp_unix=ts_sec,
                        timestamp_ms=ts_ms,
                        created_at=ts_to_dt(ts_sec),
                        text=text,
                        media=media_items,
                        reactions=reactions,
                        shared_link=share_link,
                        share_text=share_text,
                        share_owner=share_owner,
                        is_unsent=bool(raw_m.get("is_unsent")),
                        call_duration=safe_int(raw_m.get("call_duration")),
                    )
                )

        messages.sort(key=lambda m: m.timestamp_ms if m.timestamp_ms is not None else 0)
        threads.append(
            MessageThread(
                platform="instagram",
                thread_id=rel_dir,
                title=title,
                avatar_local_path=avatar_local_path,
                participants=list(participants.values()),
                is_e2ee=False,
                is_pending=is_pending,
                messages=messages,
            )
        )

    return threads


def _normalize_lv_root_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and "label_values" in data:
        return [data]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _extract_lv_bookmark_fields(lv_list: Any) -> tuple[str | None, str, str | None]:
    """Extract `(url, caption_or_text, owner)` from an Instagram `label_values` saved/repost item."""
    url: str | None = None
    text: str = ""
    owner: str | None = None
    if not isinstance(lv_list, list):
        return None, "", None

    for entry in lv_list:
        if not isinstance(entry, dict):
            continue
        lbl = entry.get("label")
        ttl = entry.get("title")
        if lbl == "URL":
            cand = entry.get("href") or entry.get("value")
            if isinstance(cand, str) and cand.strip() and not url:
                url = cand.strip()
        elif lbl in ("Caption", "Title", "Text") and isinstance(entry.get("value"), str):
            val = entry["value"].strip()
            if val and not text:
                text = val
        elif ttl == "Owner" and isinstance(entry.get("dict"), list):
            for group in _unwrap_lv_records(entry["dict"]):
                uname = None
                fname = None
                for sub in group:
                    if sub.get("label") == "Username" and sub.get("value"):
                        uname = str(sub["value"]).strip()
                    elif sub.get("label") == "Name" and sub.get("value"):
                        fname = str(sub["value"]).strip()
                if uname or fname:
                    owner = uname or fname
                    break
        elif ttl == "Media" and isinstance(entry.get("dict"), list):
            for group in _unwrap_lv_records(entry["dict"]):
                sub_url, sub_text, sub_owner = _extract_lv_bookmark_fields(group)
                url = url or sub_url
                if not text and sub_text:
                    text = sub_text
                owner = owner or sub_owner

    return url, text, owner


def _parse_ig_activities(export_root: Path) -> list[ActivityItem]:
    activities: list[ActivityItem] = []

    # 1. Own comments on posts and reels
    cmt_dir = export_root / "your_instagram_activity" / "comments"
    if cmt_dir.exists():
        cmt_sources: list[tuple[Path, str]] = [
            *((p, "post_cmt") for p in sorted(cmt_dir.glob("post_comments_*.json"))),
            (cmt_dir / "reels_comments.json", "reel_cmt"),
        ]
        for cpath, prefix in cmt_sources:
            if not cpath.exists():
                continue
            data = load_fixed_json(cpath)
            raw_list = (
                data
                if isinstance(data, list)
                else (
                    data.get("comments_media_comments")
                    or data.get("comments_reels_comments")
                    or []
                )
                if isinstance(data, dict)
                else []
            )
            if not isinstance(raw_list, list):
                continue
            for idx, entry in enumerate(raw_list):
                if not isinstance(entry, dict):
                    continue
                smap = entry.get("string_map_data")
                if not isinstance(smap, dict):
                    continue
                text = ""
                for k in ("Comment", "댓글"):
                    if isinstance(smap.get(k), dict) and isinstance(smap[k].get("value"), str):
                        text = smap[k]["value"]
                        break
                owner: str | None = None
                for k in ("Media Owner", "미디어 소유자"):
                    if isinstance(smap.get(k), dict) and isinstance(smap[k].get("value"), str):
                        owner = smap[k]["value"].strip() or None
                        break
                ts: int | None = None
                for k in ("Time", "시간"):
                    if isinstance(smap.get(k), dict):
                        ts = safe_int(smap[k].get("timestamp"))
                        if ts is not None:
                            break
                media_items: list[MediaItem] = []
                for m_entry in entry.get("media_list_data", []) if isinstance(entry.get("media_list_data"), list) else []:
                    if isinstance(m_entry, dict) and normalize_uri_if_local(m_entry.get("uri")):
                        media_items.append(media_item_from_meta_entry(m_entry))
                activities.append(
                    ActivityItem(
                        activity_id=f"ig_{prefix}_{ts or 0}_{idx}",
                        platform="instagram",
                        kind="own_comment",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        text=text,
                        target_author=owner,
                        media=media_items,
                    )
                )

    # 2. Saved posts
    saved_path = export_root / "your_instagram_activity" / "saved" / "saved_posts.json"
    if saved_path.exists():
        saved_data = load_fixed_json(saved_path)
        lv_items = _normalize_lv_root_items(saved_data)
        if lv_items:
            for idx, entry in enumerate(lv_items):
                ts = safe_int(entry.get("timestamp"))
                url, cap, owner = _extract_lv_bookmark_fields(entry.get("label_values"))
                activities.append(
                    ActivityItem(
                        activity_id=f"ig_saved_{ts or 0}_{idx}",
                        platform="instagram",
                        kind="saved_item",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        text=cap,
                        target_author=owner,
                        url=url,
                    )
                )
        elif isinstance(saved_data, dict) and isinstance(saved_data.get("saved_saved_media"), list):
            for idx, entry in enumerate(saved_data["saved_saved_media"]):
                if not isinstance(entry, dict):
                    continue
                owner = entry.get("title") if isinstance(entry.get("title"), str) else None
                smap = entry.get("string_map_data") if isinstance(entry.get("string_map_data"), dict) else {}
                saved_on = smap.get("Saved on") if isinstance(smap.get("Saved on"), dict) else {}
                url = saved_on.get("href") if isinstance(saved_on.get("href"), str) else None
                ts = safe_int(saved_on.get("timestamp"))
                activities.append(
                    ActivityItem(
                        activity_id=f"ig_saved_{ts or 0}_{idx}",
                        platform="instagram",
                        kind="saved_item",
                        created_at=ts_to_dt(ts),
                        timestamp_unix=ts,
                        target_author=owner,
                        url=url,
                    )
                )

    # 3. Reposts
    repost_path = export_root / "your_instagram_activity" / "media" / "reposts.json"
    if repost_path.exists():
        rep_data = load_fixed_json(repost_path)
        for idx, entry in enumerate(_normalize_lv_root_items(rep_data)):
            ts = safe_int(entry.get("timestamp"))
            url, cap, owner = _extract_lv_bookmark_fields(entry.get("label_values"))
            activities.append(
                ActivityItem(
                    activity_id=f"ig_repost_{ts or 0}_{idx}",
                    platform="instagram",
                    kind="repost",
                    created_at=ts_to_dt(ts),
                    timestamp_unix=ts,
                    text=cap,
                    target_author=owner,
                    url=url,
                )
            )

    return activities


def parse_ig_official_export(export_root: Path) -> OfficialExportBundle:
    """Parse an extracted Instagram official JSON export into canonical models."""
    warnings: list[str] = []
    media_json_dir = export_root / "your_instagram_activity" / "media"

    posts: list[Post] = []
    if media_json_dir.exists():
        for post_file in sorted(media_json_dir.glob("posts_*.json")):
            data = load_fixed_json(post_file)
            if isinstance(data, list):
                posts.extend(
                    _parse_legacy_post_list(data, is_archived=False, default_type="post", prefix="ig_post")
                )

        arch_path = media_json_dir / "archived_posts.json"
        if arch_path.exists():
            arch_data = load_fixed_json(arch_path)
            arch_list = arch_data.get("ig_archived_post_media") if isinstance(arch_data, dict) else arch_data
            if isinstance(arch_list, list):
                posts.extend(
                    _parse_legacy_post_list(
                        arch_list, is_archived=True, default_type="post", prefix="ig_arch"
                    )
                )

        reels_path = media_json_dir / "reels.json"
        if reels_path.exists():
            reels_data = load_fixed_json(reels_path)
            reels_list = reels_data.get("ig_reels_media") if isinstance(reels_data, dict) else reels_data
            if isinstance(reels_list, list):
                posts.extend(
                    _parse_legacy_post_list(
                        reels_list, is_archived=False, default_type="reel", prefix="ig_reel"
                    )
                )

        posts = _merge_posts_json_enrichment(posts, media_json_dir / "posts.json")

    return OfficialExportBundle(
        platform="instagram",
        root_dir=export_root,
        profile=_parse_ig_profile(export_root),
        posts=posts,
        stories=_parse_ig_stories(export_root, warnings),
        connections=_parse_ig_connections(export_root),
        messages=_parse_ig_messages(export_root),
        activities=_parse_ig_activities(export_root),
        warnings=warnings,
    )
