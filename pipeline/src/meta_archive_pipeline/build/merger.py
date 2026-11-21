"""Multi-stage perceptual/heuristic matcher, deterministic merger, and lossless materializer."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.schema.models import (
    Connection,
    LikesInfo,
    Location,
    MediaItem,
    MessageThread,
    Post,
    Profile,
    ProfileCounts,
    Story,
    UserRef,
)
from meta_archive_pipeline.schema.paths import ArchivePaths, normalize_rel_path
from meta_archive_pipeline.utils.fs import atomic_write_bytes, sha256_file, to_long_path
from meta_archive_pipeline.utils.media import (
    inspect_mp4,
    set_file_mtime,
    write_xmp_sidecar,
)


@dataclass(frozen=True)
class AmbiguousMatch:
    """Candidate match set left unmerged for manual review."""

    entity_type: Literal["post", "story", "connection"]
    platform: str
    source_id: str
    candidate_ids: list[str]
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MergeResult:
    """Unified archive dataset before/after disk materialization."""

    profiles: list[Profile] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)
    stories: list[Story] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    messages: list[MessageThread] = field(default_factory=list)
    ambiguous_matches: list[AmbiguousMatch] = field(default_factory=list)


@dataclass(frozen=True)
class ArchiveBuildSummary:
    """Summary metrics produced by `materialize_archive`."""

    output_dir: Path
    profiles_count: int
    posts_count: int
    stories_count: int
    connections_count: int
    threads_count: int
    media_copied: int
    xmp_written: int
    ambiguous_count: int


def compute_dhash(path: Path) -> int | None:
    """Compute 64-bit 8x8 horizontal difference hash (dHash) for an image file."""
    long_path = to_long_path(path)
    if not os.path.exists(long_path):
        return None
    try:
        with open(long_path, "rb") as handle:
            with Image.open(handle) as img:
                gray = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                getter = getattr(gray, "get_flattened_data", None) or gray.getdata
                pixels = list(getter())
        bits = 0
        for row in range(8):
            offset = row * 9
            for col in range(8):
                bits = (bits << 1) | (1 if pixels[offset + col] > pixels[offset + col + 1] else 0)
        return bits
    except Exception:
        return None


def hamming_distance(a: int, b: int) -> int:
    """Return Hamming bit distance between two 64-bit integer hashes."""
    return (a ^ b).bit_count()


def _resolve_source_file(rel_path: str | None, roots: Sequence[Path]) -> Path | None:
    if not rel_path:
        return None
    norm = normalize_rel_path(rel_path)
    parts = norm.split("/")
    alt_parts: list[str] | None = None
    if len(parts) >= 3 and parts[0] == "media" and parts[1] in ("instagram", "facebook"):
        alt_parts = ["media", *parts[2:]]

    for root in roots:
        cand = root.joinpath(*parts)
        if os.path.exists(to_long_path(cand)):
            return cand
        if alt_parts is not None:
            alt = root.joinpath(*alt_parts)
            if os.path.exists(to_long_path(alt)):
                return alt
    return None


def _norm_caption(text: str) -> str:
    return " ".join(text.split()).lower()


def _non_cover_media(post: Post) -> list[MediaItem]:
    non_cover = [m for m in post.media if not m.is_carousel_cover]
    return non_cover if non_cover else list(post.media)


def _post_dhashes(post: Post, roots: Sequence[Path]) -> list[int]:
    hashes: list[int] = []
    for m in _non_cover_media(post):
        if m.media_type in ("image", "unknown") and m.local_path:
            resolved = _resolve_source_file(m.local_path, roots)
            if resolved is not None:
                h = compute_dhash(resolved)
                if h is not None:
                    hashes.append(h)
    return hashes


def _min_dhash_distance(hashes_a: Sequence[int], hashes_b: Sequence[int]) -> int | None:
    if not hashes_a or not hashes_b:
        return None
    return min(hamming_distance(ha, hb) for ha in hashes_a for hb in hashes_b)


def _mp4_has_audio_on_disk(media: MediaItem, roots: Sequence[Path]) -> bool | None:
    if media.has_audio is not None:
        return media.has_audio
    resolved = _resolve_source_file(media.local_path, roots)
    if resolved is None:
        return None
    res = inspect_mp4(resolved)
    return res.has_audio if res.ok else None


def _merge_location(off_loc: Location | None, pw_loc: Location | None) -> Location | None:
    if off_loc is None:
        return pw_loc
    if pw_loc is None:
        return off_loc
    return Location(
        location_id=pw_loc.location_id or off_loc.location_id,
        name=pw_loc.name or off_loc.name,
        description=pw_loc.description or off_loc.description,
        address=pw_loc.address or off_loc.address,
        lat=pw_loc.lat if pw_loc.lat is not None else off_loc.lat,
        lng=pw_loc.lng if pw_loc.lng is not None else off_loc.lng,
    )


def _merge_media_lists(
    off_media: Sequence[MediaItem],
    pw_media: Sequence[MediaItem],
    official_roots: Sequence[Path],
    playwright_roots: Sequence[Path],
) -> list[MediaItem]:
    if not pw_media:
        return list(off_media)
    if not off_media:
        return list(pw_media)

    off_noncover = [m for m in off_media if not m.is_carousel_cover] or list(off_media)
    merged: list[MediaItem] = []

    for idx, pw_item in enumerate(pw_media):
        off_match: MediaItem | None = None
        if pw_item.media_id:
            off_match = next((m for m in off_media if m.media_id == pw_item.media_id), None)
        if off_match is None and idx < len(off_noncover):
            off_match = off_noncover[idx]

        chosen = pw_item.model_copy()
        if off_match is not None:
            if not chosen.taken_at_exif and off_match.taken_at_exif:
                chosen.taken_at_exif = off_match.taken_at_exif
            if chosen.gps_lat is None and off_match.gps_lat is not None:
                chosen.gps_lat = off_match.gps_lat
            if chosen.gps_lng is None and off_match.gps_lng is not None:
                chosen.gps_lng = off_match.gps_lng
            if chosen.is_audio_muted is None and off_match.is_audio_muted is not None:
                chosen.is_audio_muted = off_match.is_audio_muted

            if chosen.media_type == "video" and off_match.media_type == "video":
                pw_audio = _mp4_has_audio_on_disk(chosen, playwright_roots)
                off_audio = _mp4_has_audio_on_disk(off_match, official_roots)
                # Prefer official only if Playwright somehow failed audio while official has audio;
                # otherwise Playwright audio (or standard web MP4) wins.
                if pw_audio is False and off_audio is True and off_match.local_path:
                    chosen = off_match.model_copy(
                        update={
                            "taken_at_exif": chosen.taken_at_exif or off_match.taken_at_exif,
                            "gps_lat": chosen.gps_lat if chosen.gps_lat is not None else off_match.gps_lat,
                            "gps_lng": chosen.gps_lng if chosen.gps_lng is not None else off_match.gps_lng,
                        }
                    )

        merged.append(chosen)

    return merged


def _merge_two_posts(
    off: Post,
    pw: Post,
    official_roots: Sequence[Path],
    playwright_roots: Sequence[Path],
) -> Post:
    author = UserRef(
        user_id=pw.author.user_id or off.author.user_id,
        username=pw.author.username or off.author.username,
        full_name=pw.author.full_name or off.author.full_name,
        profile_url=pw.author.profile_url or off.author.profile_url,
        avatar_local_path=pw.author.avatar_local_path or off.author.avatar_local_path,
    )
    hashtags = list(dict.fromkeys([*pw.hashtags, *off.hashtags]))
    merged_media = _merge_media_lists(off.media, pw.media, official_roots, playwright_roots)
    post_type = pw.post_type
    if off.post_type == "album_photo" and post_type == "post":
        post_type = "album_photo"
    elif off.post_type == "carousel" and len(merged_media) > 1:
        post_type = "carousel"

    return Post(
        platform=pw.platform,
        post_id=pw.post_id or off.post_id,
        shortcode=pw.shortcode or off.shortcode,
        url=pw.url or off.url,
        post_type=post_type,
        author=author,
        created_at=pw.created_at or off.created_at,
        timestamp_unix=pw.timestamp_unix or off.timestamp_unix,
        caption=pw.caption or off.caption,
        hashtags=hashtags,
        location=_merge_location(off.location, pw.location),
        tagged_users=pw.tagged_users or off.tagged_users,
        media=merged_media,
        likes=pw.likes if (pw.likes.count is not None or pw.likes.users) else off.likes,
        reactions=pw.reactions if pw.reactions.total > 0 else off.reactions,
        comments=pw.comments or off.comments,
        album_name=pw.album_name or off.album_name,
        shared_post=pw.shared_post or off.shared_post,
        audience=pw.audience or off.audience,
        is_archived=off.is_archived or pw.is_archived,
        is_collab=pw.is_collab or off.is_collab,
        source="merged",
    )


def _score_post_pair(
    off: Post,
    pw: Post,
    off_hashes: Sequence[int],
    pw_hashes: Sequence[int],
) -> tuple[float, dict[str, Any]] | None:
    ts_delta: int | None = None
    score = 0.0

    if off.timestamp_unix and pw.timestamp_unix:
        ts_delta = abs(off.timestamp_unix - pw.timestamp_unix)
        if ts_delta > 86400:
            return None
        if ts_delta <= 60:
            score += 3.0
        elif ts_delta <= 3600:
            score += 1.0

    cap_sim = 0.0
    off_cap = _norm_caption(off.caption)
    pw_cap = _norm_caption(pw.caption)
    if off_cap and pw_cap:
        cap_sim = SequenceMatcher(None, off_cap, pw_cap).ratio()
        if cap_sim >= 0.85:
            score += 3.0
        elif cap_sim >= 0.60:
            score += 1.5

    dhash_dist = _min_dhash_distance(off_hashes, pw_hashes)
    if dhash_dist is not None and dhash_dist <= 10:
        score += 4.0

    off_cnt = len(_non_cover_media(off))
    pw_cnt = len(_non_cover_media(pw))
    if off_cnt > 0 and off_cnt == pw_cnt:
        score += 1.0

    if score < 2.5:
        return None

    return score, {
        "score": round(score, 2),
        "ts_delta": ts_delta,
        "caption_similarity": round(cap_sim, 3),
        "dhash_distance": dhash_dist,
    }


def merge_posts(
    official_posts: Sequence[Post],
    playwright_posts: Sequence[Post],
    *,
    official_roots: Sequence[Path] = (),
    playwright_roots: Sequence[Path] = (),
) -> tuple[list[Post], list[AmbiguousMatch]]:
    """Match and merge official and Playwright posts using exact keys and multi-stage scoring."""
    merged_out: list[Post] = []
    ambiguous: list[AmbiguousMatch] = []

    platforms = sorted({*(p.platform for p in official_posts), *(p.platform for p in playwright_posts)})
    for platform in platforms:
        off_list = [p for p in official_posts if p.platform == platform]
        pw_list = [p for p in playwright_posts if p.platform == platform]

        used_off: set[int] = set()
        used_pw: set[int] = set()

        # Stage 1: Exact key match (shortcode, post_id, or shared media_id)
        pw_by_shortcode = {p.shortcode: idx for idx, p in enumerate(pw_list) if p.shortcode}
        pw_by_post_id = {p.post_id: idx for idx, p in enumerate(pw_list) if p.post_id}
        pw_by_media_id: dict[str, int] = {}
        for idx, p in enumerate(pw_list):
            for m in p.media:
                if m.media_id:
                    pw_by_media_id.setdefault(m.media_id, idx)

        for oi, off in enumerate(off_list):
            match_pi: int | None = None
            if off.shortcode and off.shortcode in pw_by_shortcode:
                match_pi = pw_by_shortcode[off.shortcode]
            elif off.post_id in pw_by_post_id:
                match_pi = pw_by_post_id[off.post_id]
            else:
                for m in off.media:
                    if m.media_id and m.media_id in pw_by_media_id:
                        match_pi = pw_by_media_id[m.media_id]
                        break

            if match_pi is not None and match_pi not in used_pw:
                used_off.add(oi)
                used_pw.add(match_pi)
                merged_out.append(
                    _merge_two_posts(off, pw_list[match_pi], official_roots, playwright_roots)
                )

        # Stage 2: Multi-stage heuristic + dHash scoring on remaining unmatched posts
        rem_off = [(oi, off_list[oi]) for oi in range(len(off_list)) if oi not in used_off]
        rem_pw = [(pi, pw_list[pi]) for pi in range(len(pw_list)) if pi not in used_pw]

        off_hash_map = {oi: _post_dhashes(p, official_roots) for oi, p in rem_off}
        pw_hash_map = {pi: _post_dhashes(p, playwright_roots) for pi, p in rem_pw}

        strong_candidates: dict[int, list[tuple[int, float, dict[str, Any]]]] = {}
        borderline_candidates: dict[int, list[tuple[int, float, dict[str, Any]]]] = {}
        pw_claim_scores: dict[int, list[tuple[int, float]]] = {}

        for oi, off in rem_off:
            for pi, pw in rem_pw:
                scored = _score_post_pair(off, pw, off_hash_map[oi], pw_hash_map[pi])
                if scored is None:
                    continue
                score, info = scored
                if score >= 4.0:
                    strong_candidates.setdefault(oi, []).append((pi, score, info))
                    pw_claim_scores.setdefault(pi, []).append((oi, score))
                else:
                    borderline_candidates.setdefault(oi, []).append((pi, score, info))

        for oi, off in rem_off:
            strong = sorted(strong_candidates.get(oi, []), key=lambda x: x[1], reverse=True)
            if strong:
                best_pi, best_score, _best_info = strong[0]
                close_runner_up = len(strong) > 1 and (best_score - strong[1][1]) < 1.5
                contested_pw = (
                    len(
                        [
                            1
                            for other_oi, s in pw_claim_scores.get(best_pi, [])
                            if other_oi != oi and abs(s - best_score) < 1.5
                        ]
                    )
                    > 0
                )
                if not close_runner_up and not contested_pw and best_pi not in used_pw:
                    used_off.add(oi)
                    used_pw.add(best_pi)
                    merged_out.append(
                        _merge_two_posts(off, pw_list[best_pi], official_roots, playwright_roots)
                    )
                else:
                    ambiguous.append(
                        AmbiguousMatch(
                            entity_type="post",
                            platform=platform,
                            source_id=off.post_id,
                            candidate_ids=[pw_list[pi].post_id for pi, _, _ in strong],
                            reason="tied_or_contested_post_match",
                            details={"candidates": [info for _, _, info in strong]},
                        )
                    )
            elif oi in borderline_candidates:
                border = borderline_candidates[oi]
                ambiguous.append(
                    AmbiguousMatch(
                        entity_type="post",
                        platform=platform,
                        source_id=off.post_id,
                        candidate_ids=[pw_list[pi].post_id for pi, _, _ in border],
                        reason="borderline_post_match",
                        details={"candidates": [info for _, _, info in border]},
                    )
                )

        # Keep remaining unmerged posts from both sides
        for oi, off in enumerate(off_list):
            if oi not in used_off:
                merged_out.append(off)
        for pi, pw in enumerate(pw_list):
            if pi not in used_pw:
                merged_out.append(pw)

    merged_out.sort(key=lambda p: p.timestamp_unix if p.timestamp_unix is not None else 0, reverse=True)
    return merged_out, ambiguous


def merge_stories(
    official_stories: Sequence[Story],
    playwright_stories: Sequence[Story],
    *,
    official_roots: Sequence[Path] = (),
    playwright_roots: Sequence[Path] = (),
) -> list[Story]:
    """Merge official archive stories with Playwright web highlights."""
    used_pw: set[int] = set()
    pw_by_id = {s.story_id: i for i, s in enumerate(playwright_stories)}
    out: list[Story] = []

    for off in official_stories:
        match_idx = pw_by_id.get(off.story_id)
        if match_idx is None and off.timestamp_unix:
            off_hash = (
                compute_dhash(resolved)
                if off.media and (resolved := _resolve_source_file(off.media.local_path, official_roots))
                else None
            )
            for pi, pw in enumerate(playwright_stories):
                if pi in used_pw or pw.platform != off.platform or not pw.timestamp_unix:
                    continue
                if abs(off.timestamp_unix - pw.timestamp_unix) <= 60:
                    if off_hash is not None and pw.media:
                        pw_res = _resolve_source_file(pw.media.local_path, playwright_roots)
                        pw_hash = compute_dhash(pw_res) if pw_res else None
                        if pw_hash is not None and hamming_distance(off_hash, pw_hash) > 10:
                            continue
                    match_idx = pi
                    break

        if match_idx is not None and match_idx not in used_pw:
            used_pw.add(match_idx)
            pw = playwright_stories[match_idx]
            media = off.media or pw.media
            if off.media and pw.media:
                media = off.media.model_copy(
                    update={
                        "local_path": off.media.local_path or pw.media.local_path,
                        "taken_at_exif": off.media.taken_at_exif or pw.media.taken_at_exif,
                        "gps_lat": off.media.gps_lat if off.media.gps_lat is not None else pw.media.gps_lat,
                        "gps_lng": off.media.gps_lng if off.media.gps_lng is not None else pw.media.gps_lng,
                    }
                )
            out.append(
                Story(
                    platform=off.platform,
                    story_id=off.story_id,
                    created_at=off.created_at or pw.created_at,
                    timestamp_unix=off.timestamp_unix or pw.timestamp_unix,
                    source="merged",
                    highlight_id=pw.highlight_id or off.highlight_id,
                    highlight_title=pw.highlight_title or off.highlight_title,
                    highlight_cover=pw.highlight_cover or off.highlight_cover,
                    media=media,
                    location=pw.location or off.location,
                    stickers=pw.stickers or off.stickers,
                    likes_count=pw.likes_count if pw.likes_count is not None else off.likes_count,
                )
            )
        else:
            out.append(off)

    for pi, pw in enumerate(playwright_stories):
        if pi not in used_pw:
            out.append(pw)

    out.sort(key=lambda s: s.timestamp_unix if s.timestamp_unix is not None else 0, reverse=True)
    return out


def merge_connections(
    official_conns: Sequence[Connection],
    playwright_conns: Sequence[Connection],
) -> tuple[list[Connection], list[AmbiguousMatch]]:
    """Join official connections with Playwright profile URLs, full names, and avatars."""
    merged: list[Connection] = []
    ambiguous: list[AmbiguousMatch] = []

    # Instagram: exact (relation, lower_username) join
    ig_pw_map: dict[tuple[str, str], Connection] = {
        (c.relation, c.username.lower()): c
        for c in playwright_conns
        if c.platform == "instagram" and c.username
    }
    ig_pw_used: set[tuple[str, str]] = set()
    for off in [c for c in official_conns if c.platform == "instagram"]:
        key = (off.relation, off.username.lower()) if off.username else None
        if key and key in ig_pw_map:
            ig_pw_used.add(key)
            pw = ig_pw_map[key]
            merged.append(
                Connection(
                    platform="instagram",
                    relation=off.relation,
                    user_id=pw.user_id or off.user_id,
                    username=off.username or pw.username,
                    full_name=pw.full_name or off.full_name,
                    profile_url=pw.profile_url or off.profile_url,
                    avatar_local_path=pw.avatar_local_path or off.avatar_local_path,
                    connected_at=off.connected_at or pw.connected_at,
                    timestamp_unix=off.timestamp_unix or pw.timestamp_unix,
                    match_confidence="exact",
                )
            )
        else:
            merged.append(off)

    for key, pw in ig_pw_map.items():
        if key not in ig_pw_used:
            merged.append(pw)

    # Facebook: unique 1:1 full_name heuristic join; duplicate names flagged, never force-merged
    fb_off = [c for c in official_conns if c.platform == "facebook"]
    fb_pw = [c for c in playwright_conns if c.platform == "facebook"]

    def _norm_name(c: Connection) -> str:
        return " ".join((c.full_name or "").split()).lower()

    off_by_name: dict[str, list[Connection]] = {}
    pw_by_name: dict[str, list[Connection]] = {}
    for c in fb_off:
        off_by_name.setdefault(_norm_name(c), []).append(c)
    for c in fb_pw:
        pw_by_name.setdefault(_norm_name(c), []).append(c)

    for name_key in sorted(set(off_by_name) | set(pw_by_name)):
        off_group = off_by_name.get(name_key, [])
        pw_group = pw_by_name.get(name_key, [])
        if name_key and len(off_group) == 1 and len(pw_group) == 1:
            o, p = off_group[0], pw_group[0]
            merged.append(
                Connection(
                    platform="facebook",
                    relation=o.relation,
                    user_id=p.user_id or o.user_id,
                    username=p.username or o.username,
                    full_name=p.full_name or o.full_name,
                    profile_url=p.profile_url or o.profile_url,
                    avatar_local_path=p.avatar_local_path or o.avatar_local_path,
                    connected_at=o.connected_at or p.connected_at,
                    timestamp_unix=o.timestamp_unix or p.timestamp_unix,
                    match_confidence="heuristic",
                )
            )
        else:
            if name_key and off_group and pw_group:
                ambiguous.append(
                    AmbiguousMatch(
                        entity_type="connection",
                        platform="facebook",
                        source_id=off_group[0].full_name or name_key,
                        candidate_ids=[c.user_id or c.profile_url or c.full_name or "" for c in pw_group],
                        reason="duplicate_facebook_friend_name",
                        details={"official_count": len(off_group), "playwright_count": len(pw_group)},
                    )
                )
            for c in off_group:
                merged.append(c.model_copy(update={"match_confidence": "unmatched"}))
            for c in pw_group:
                merged.append(c.model_copy(update={"match_confidence": "unmatched"}))

    return merged, ambiguous


def merge_profiles(
    official_profiles: Sequence[Profile],
    playwright_profiles: Sequence[Profile],
) -> list[Profile]:
    """Merge profiles per platform, keeping live counts and bio/avatar details."""
    by_platform: dict[str, Profile] = {}
    for p in official_profiles:
        by_platform[p.platform] = p
    for pw in playwright_profiles:
        off = by_platform.get(pw.platform)
        if off is None:
            by_platform[pw.platform] = pw
        else:
            by_platform[pw.platform] = Profile(
                platform=pw.platform,
                user_id=pw.user_id or off.user_id,
                username=pw.username or off.username,
                full_name=pw.full_name or off.full_name,
                bio=pw.bio or off.bio,
                avatar_local_path=pw.avatar_local_path or off.avatar_local_path,
                counts=ProfileCounts(
                    posts=pw.counts.posts if pw.counts.posts is not None else off.counts.posts,
                    followers=pw.counts.followers if pw.counts.followers is not None else off.counts.followers,
                    following=pw.counts.following if pw.counts.following is not None else off.counts.following,
                    friends=pw.counts.friends if pw.counts.friends is not None else off.counts.friends,
                ),
                exported_at=pw.exported_at or off.exported_at,
            )
    return list(by_platform.values())


def merge_all(
    *,
    official_bundles: Sequence[OfficialExportBundle] = (),
    collected_profiles: Sequence[Profile] = (),
    collected_posts: Sequence[Post] = (),
    collected_stories: Sequence[Story] = (),
    collected_connections: Sequence[Connection] = (),
    collected_roots: Sequence[Path] = (),
) -> MergeResult:
    """Merge all official bundles and collected Playwright artifacts into a unified MergeResult."""
    off_roots = [r for b in official_bundles for r in (b.root_dir, *b.extra_roots)]
    off_profiles = [b.profile for b in official_bundles if b.profile is not None]
    off_posts = [p for b in official_bundles for p in b.posts]
    off_stories = [s for b in official_bundles for s in b.stories]
    off_conns = [c for b in official_bundles for c in b.connections]
    messages = [t for b in official_bundles for t in b.messages]

    profiles = merge_profiles(off_profiles, collected_profiles)
    posts, post_amb = merge_posts(
        off_posts,
        collected_posts,
        official_roots=off_roots,
        playwright_roots=collected_roots,
    )
    stories = merge_stories(
        off_stories,
        collected_stories,
        official_roots=off_roots,
        playwright_roots=collected_roots,
    )
    connections, conn_amb = merge_connections(off_conns, collected_connections)

    return MergeResult(
        profiles=profiles,
        posts=posts,
        stories=stories,
        connections=connections,
        messages=messages,
        ambiguous_matches=[*post_amb, *conn_amb],
    )


def _copy_file_lossless(src: Path, dest: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    src_long = to_long_path(src)
    dest_long = to_long_path(dest)
    os.makedirs(to_long_path(dest.parent), exist_ok=True)

    digest = hashlib.sha256()
    total = 0
    tmp_path: str | None = None
    try:
        with (
            open(src_long, "rb") as fin,
            tempfile.NamedTemporaryFile(
                mode="wb",
                dir=to_long_path(dest.parent),
                prefix=f".{dest.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp,
        ):
            tmp_path = tmp.name
            while chunk := fin.read(chunk_size):
                digest.update(chunk)
                tmp.write(chunk)
                total += len(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, dest_long)
        tmp_path = None
        return digest.hexdigest(), total
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _canonical_media_rel(platform: str, category: str, src_rel: str) -> str:
    norm = normalize_rel_path(src_rel)
    parts = norm.split("/")
    # Strip leading media/<platform>/<category>/ or media/<category>/ if already present
    if len(parts) >= 4 and parts[0] == "media" and parts[1] == platform and parts[2] == category:
        tail = "/".join(parts[3:])
    elif len(parts) >= 3 and parts[0] == "media":
        tail = "/".join(parts[2:])
    else:
        tail = parts[-1]
    return ArchivePaths.media_rel_path(platform, category, tail)


def materialize_archive(
    merge_result: MergeResult,
    output_dir: Path,
    *,
    source_roots: Sequence[Path] = (),
    tz_offset: str = "+09:00",
) -> ArchiveBuildSummary:
    """Materialize merged media (with XMP sidecars + mtime) and canonical JSON into output_dir."""
    paths = ArchivePaths(output_dir)
    os.makedirs(to_long_path(paths.data_dir), exist_ok=True)
    os.makedirs(to_long_path(paths.media_dir), exist_ok=True)
    os.makedirs(to_long_path(paths.reports_dir), exist_ok=True)

    copied_cache: dict[str, tuple[str, str, int]] = {}  # src_long -> (dest_rel, sha256, size)
    media_copied = 0
    xmp_written = 0

    def _materialize_media_file(
        src_rel: str | None,
        platform: str,
        category: str,
    ) -> tuple[str | None, str | None, int | None, Path | None]:
        nonlocal media_copied
        if not src_rel:
            return None, None, None, None
        src_file = _resolve_source_file(src_rel, source_roots)
        if src_file is None:
            return src_rel, None, None, None

        src_key = to_long_path(src_file)
        if src_key in copied_cache:
            dest_rel, sha, sz = copied_cache[src_key]
            return dest_rel, sha, sz, paths.root.joinpath(*dest_rel.split("/"))

        dest_rel = _canonical_media_rel(platform, category, src_rel)
        dest_file = paths.root.joinpath(*dest_rel.split("/"))
        if os.path.exists(to_long_path(dest_file)):
            sha = sha256_file(dest_file)
            sz = os.path.getsize(to_long_path(dest_file))
        else:
            sha, sz = _copy_file_lossless(src_file, dest_file)
            media_copied += 1

        copied_cache[src_key] = (dest_rel, sha, sz)
        return dest_rel, sha, sz, dest_file

    def _process_media_item(
        item: MediaItem,
        platform: str,
        category: str,
        *,
        ts_unix: int | None,
        lat: float | None = None,
        lng: float | None = None,
        description: str | None = None,
        write_xmp: bool = True,
    ) -> None:
        dest_rel, sha, sz, dest_file = _materialize_media_file(item.local_path, platform, category)
        if dest_rel:
            item.local_path = dest_rel
        if sha is not None:
            item.sha256 = sha
        if sz is not None:
            item.byte_size = sz
        if dest_file is not None:
            eff_ts = ts_unix
            if write_xmp and (eff_ts is not None or item.taken_at_exif):
                nonlocal xmp_written
                sidecar = write_xmp_sidecar(
                    dest_file,
                    timestamp_unix=eff_ts,
                    exif_datetime_original=item.taken_at_exif,
                    tz_offset=tz_offset,
                    gps_lat=item.gps_lat if item.gps_lat is not None else lat,
                    gps_lng=item.gps_lng if item.gps_lng is not None else lng,
                    description=description,
                )
                if sidecar is not None:
                    item.xmp_path = normalize_rel_path(
                        sidecar.relative_to(paths.root).as_posix()
                    )
                    xmp_written += 1
            if eff_ts is not None:
                set_file_mtime(dest_file, eff_ts)

    for profile in merge_result.profiles:
        if profile.avatar_local_path:
            dest_rel, _, _, _ = _materialize_media_file(
                profile.avatar_local_path, profile.platform, "profile"
            )
            profile.avatar_local_path = dest_rel

    for post in merge_result.posts:
        post_lat = post.location.lat if post.location else None
        post_lng = post.location.lng if post.location else None
        for m in post.media:
            _process_media_item(
                m,
                post.platform,
                "posts",
                ts_unix=post.timestamp_unix,
                lat=post_lat,
                lng=post_lng,
                description=post.caption or None,
                write_xmp=True,
            )

    for story in merge_result.stories:
        if story.highlight_cover:
            dest_rel, _, _, _ = _materialize_media_file(
                story.highlight_cover, story.platform, "highlights"
            )
            story.highlight_cover = dest_rel
        if story.media is not None:
            s_lat = story.location.lat if story.location else None
            s_lng = story.location.lng if story.location else None
            _process_media_item(
                story.media,
                story.platform,
                "stories",
                ts_unix=story.timestamp_unix,
                lat=s_lat,
                lng=s_lng,
                description=story.highlight_title,
                write_xmp=True,
            )

    for conn in merge_result.connections:
        if conn.avatar_local_path:
            dest_rel, _, _, _ = _materialize_media_file(
                conn.avatar_local_path, conn.platform, "avatars"
            )
            conn.avatar_local_path = dest_rel

    for thread in merge_result.messages:
        for msg in thread.messages:
            for m in msg.media:
                _process_media_item(
                    m,
                    thread.platform,
                    "messages",
                    ts_unix=msg.timestamp_unix,
                    write_xmp=True,
                )

    def _dump_json(path: Path, data: Any) -> None:
        atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    _dump_json(paths.profiles_json, [p.model_dump(mode="json") for p in merge_result.profiles])
    _dump_json(paths.posts_json, [p.model_dump(mode="json") for p in merge_result.posts])
    _dump_json(paths.stories_json, [s.model_dump(mode="json") for s in merge_result.stories])
    _dump_json(paths.connections_json, [c.model_dump(mode="json") for c in merge_result.connections])
    _dump_json(paths.messages_json, [t.model_dump(mode="json") for t in merge_result.messages])
    _dump_json(
        paths.ambiguous_matches_path,
        [
            {
                "entity_type": a.entity_type,
                "platform": a.platform,
                "source_id": a.source_id,
                "candidate_ids": a.candidate_ids,
                "reason": a.reason,
                "details": a.details,
            }
            for a in merge_result.ambiguous_matches
        ],
    )

    return ArchiveBuildSummary(
        output_dir=output_dir,
        profiles_count=len(merge_result.profiles),
        posts_count=len(merge_result.posts),
        stories_count=len(merge_result.stories),
        connections_count=len(merge_result.connections),
        threads_count=len(merge_result.messages),
        media_copied=media_copied,
        xmp_written=xmp_written,
        ambiguous_count=len(merge_result.ambiguous_matches),
    )
