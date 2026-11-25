"""Deterministic in-place archive loader, FB stub deduplicator, and incremental delta patcher."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meta_archive_pipeline.build.indexer import (
    build_sqlite_index,
    verify_manifest_sha256,
    write_manifest_sha256,
)
from meta_archive_pipeline.build.merger import (
    AmbiguousMatch,
    _copy_file_lossless,
    _merge_two_posts,
    _sniff_normalized_ext,
    merge_connections,
    merge_posts,
    merge_profiles,
    merge_stories,
)
from meta_archive_pipeline.schema.models import (
    ActivityItem,
    Comment,
    Connection,
    LikesInfo,
    MediaItem,
    Message,
    MessageThread,
    Post,
    Profile,
    Story,
    UserRef,
)
from meta_archive_pipeline.schema.paths import ArchivePaths, normalize_rel_path
from meta_archive_pipeline.utils.fs import atomic_write_bytes, sha256_file, to_long_path
from meta_archive_pipeline.utils.media import inspect_mp4, set_file_mtime, write_xmp_sidecar


@dataclass(frozen=True)
class MissingDmMediaTarget:
    """An archived DM media item whose `local_path` is missing (only remote `original_uri`)."""

    platform: str
    thread_id: str
    thread_title: str | None
    message_index: int
    media_index: int
    timestamp_ms: int | None
    sender: str
    original_uri: str


@dataclass(frozen=True)
class SilentVideoTarget:
    """An archived post video currently missing an audio track (`has_audio is False`)."""

    platform: str
    post_id: str
    shortcode: str
    media_index: int
    local_path: str
    created_at: str | None = None


@dataclass
class ArchiveBaseline:
    """Snapshot of an existing built archive used to drive incremental CDP sync & repairs."""

    archive_dir: Path
    profiles: list[Profile]
    posts: list[Post]
    stories: list[Story]
    connections: list[Connection]
    messages: list[MessageThread]
    activities: list[ActivityItem]
    ambiguous_matches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ig_profile(self) -> Profile | None:
        return next((p for p in self.profiles if p.platform == "instagram"), None)

    @property
    def fb_profile(self) -> Profile | None:
        return next((p for p in self.profiles if p.platform == "facebook"), None)

    def latest_message_ms(self, platform: str) -> int:
        best = 0
        for thread in self.messages:
            if thread.platform != platform:
                continue
            for msg in thread.messages:
                ms = _message_ts_ms(msg)
                if ms > best:
                    best = ms
        return best

    def latest_post_unix(self, platform: str) -> int:
        best = 0
        for post in self.posts:
            if post.platform == platform and post.timestamp_unix and post.timestamp_unix > best:
                best = post.timestamp_unix
        return best

    def known_shortcodes(self, platform: str = "instagram") -> set[str]:
        return {p.shortcode for p in self.posts if p.platform == platform and p.shortcode}

    def known_post_ids(self, platform: str) -> set[str]:
        return {p.post_id for p in self.posts if p.platform == platform}

    def known_story_ids(self, platform: str) -> set[str]:
        return {s.story_id for s in self.stories if s.platform == platform}

    def missing_dm_media(self, platform: str = "instagram") -> list[MissingDmMediaTarget]:
        targets: list[MissingDmMediaTarget] = []
        for thread in self.messages:
            if thread.platform != platform:
                continue
            for mi, msg in enumerate(thread.messages):
                for med_i, med in enumerate(msg.media):
                    if not med.local_path and med.original_uri:
                        targets.append(
                            MissingDmMediaTarget(
                                platform=platform,
                                thread_id=thread.thread_id,
                                thread_title=thread.title,
                                message_index=mi,
                                media_index=med_i,
                                timestamp_ms=_message_ts_ms(msg) or None,
                                sender=msg.sender,
                                original_uri=med.original_uri,
                            )
                        )
        return targets

    def silent_post_videos(self, platform: str = "instagram") -> list[SilentVideoTarget]:
        targets: list[SilentVideoTarget] = []
        for post in self.posts:
            if post.platform != platform or not post.shortcode:
                continue
            for idx, med in enumerate(post.media):
                if (
                    med.media_type == "video"
                    and med.has_audio is False
                    and med.is_audio_muted is not True
                    and med.local_path
                ):
                    targets.append(
                        SilentVideoTarget(
                            platform=platform,
                            post_id=post.post_id,
                            shortcode=post.shortcode,
                            media_index=idx,
                            local_path=med.local_path,
                            created_at=post.created_at.isoformat() if post.created_at else None,
                        )
                    )
        return targets


@dataclass
class DmMediaRepair:
    """Recovered local file for a previously missing DM media item."""

    thread_id: str
    message_index: int
    media_index: int
    staged_file: Path
    media_type: str = "image"
    width: int | None = None
    height: int | None = None


@dataclass
class VideoAudioRepair:
    """Outcome of checking/repairing audio on a silent post video."""

    post_id: str
    shortcode: str
    media_index: int
    status: str  # "repaired" | "confirmed_silent" | "unavailable" | "error"
    staged_file: Path | None = None
    detail: str = ""


@dataclass
class SyncDelta:
    """Incremental additions and targeted repairs collected via Playwright CDP."""

    profiles: list[Profile] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)
    stories: list[Story] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    message_threads: list[MessageThread] = field(default_factory=list)
    dm_media_repairs: list[DmMediaRepair] = field(default_factory=list)
    video_audio_repairs: list[VideoAudioRepair] = field(default_factory=list)
    staged_roots: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PatchSummary:
    """Summary of changes (dry-run or applied) produced by `apply_sync_delta`."""

    applied: bool
    backup_dir: Path | None = None
    fb_stubs_merged: int = 0
    fb_ambiguous_remaining: int = 0
    ig_new_messages: int = 0
    ig_updated_threads: int = 0
    ig_new_threads: int = 0
    fb_new_messages: int = 0
    fb_updated_threads: int = 0
    fb_new_threads: int = 0
    dm_media_repaired: int = 0
    videos_audio_repaired: int = 0
    videos_confirmed_silent: int = 0
    posts_added: int = 0
    posts_updated: int = 0
    stories_added: int = 0
    connections_added: int = 0
    media_copied: int = 0
    xmp_written: int = 0
    manifest_verified: int = 0
    manifest_failures: list[str] = field(default_factory=list)
    thread_previews: list[dict[str, Any]] = field(default_factory=list)
    video_outcomes: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "fb_stubs_merged": self.fb_stubs_merged,
            "fb_ambiguous_remaining": self.fb_ambiguous_remaining,
            "ig_new_messages": self.ig_new_messages,
            "ig_updated_threads": self.ig_updated_threads,
            "ig_new_threads": self.ig_new_threads,
            "fb_new_messages": self.fb_new_messages,
            "fb_updated_threads": self.fb_updated_threads,
            "fb_new_threads": self.fb_new_threads,
            "dm_media_repaired": self.dm_media_repaired,
            "videos_audio_repaired": self.videos_audio_repaired,
            "videos_confirmed_silent": self.videos_confirmed_silent,
            "posts_added": self.posts_added,
            "posts_updated": self.posts_updated,
            "stories_added": self.stories_added,
            "connections_added": self.connections_added,
            "media_copied": self.media_copied,
            "xmp_written": self.xmp_written,
            "manifest_verified": self.manifest_verified,
            "manifest_failures": self.manifest_failures,
            "thread_previews": self.thread_previews,
            "video_outcomes": self.video_outcomes,
            "notes": self.notes,
        }


def _message_ts_ms(msg: Message) -> int:
    if msg.timestamp_ms is not None and msg.timestamp_ms > 0:
        return int(msg.timestamp_ms)
    if msg.timestamp_unix is not None and msg.timestamp_unix > 0:
        return int(msg.timestamp_unix) * 1000
    if msg.created_at is not None:
        return int(msg.created_at.timestamp() * 1000)
    return 0


def _load_json_list(path: Path) -> list[Any]:
    lp = to_long_path(path)
    if not os.path.exists(lp):
        return []
    with open(lp, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else []


def load_archive_baseline(archive_dir: Path) -> ArchiveBaseline:
    """Load canonical Pydantic records from an existing built archive directory."""
    paths = ArchivePaths(archive_dir)
    if not paths.data_dir.exists():
        raise FileNotFoundError(f"Archive data directory not found: {paths.data_dir}")

    profiles = [Profile.model_validate(x) for x in _load_json_list(paths.profiles_json)]
    posts = [Post.model_validate(x) for x in _load_json_list(paths.posts_json)]
    stories = [Story.model_validate(x) for x in _load_json_list(paths.stories_json)]
    connections = [Connection.model_validate(x) for x in _load_json_list(paths.connections_json)]
    messages = [MessageThread.model_validate(x) for x in _load_json_list(paths.messages_json)]
    activities = [ActivityItem.model_validate(x) for x in _load_json_list(paths.activities_json)]
    ambiguous = _load_json_list(paths.ambiguous_matches_path)

    return ArchiveBaseline(
        archive_dir=archive_dir,
        profiles=profiles,
        posts=posts,
        stories=stories,
        connections=connections,
        messages=messages,
        activities=activities,
        ambiguous_matches=ambiguous,
    )


def dedup_fb_posts_in_archive(
    posts: Sequence[Post],
    *,
    archive_root: Path,
) -> tuple[list[Post], int, list[AmbiguousMatch]]:
    """Re-run `merge_posts` on unmerged Facebook official/playwright posts already in an archive."""
    fb_off = [p for p in posts if p.platform == "facebook" and p.source == "official_export"]
    fb_pw = [p for p in posts if p.platform == "facebook" and p.source == "playwright"]
    keep = [
        p
        for p in posts
        if not (p.platform == "facebook" and p.source in ("official_export", "playwright"))
    ]
    if not fb_off or not fb_pw:
        return list(posts), 0, []

    merged_fb, amb = merge_posts(
        fb_off,
        fb_pw,
        official_roots=[archive_root],
        playwright_roots=[archive_root],
    )
    newly_merged = sum(1 for p in merged_fb if p.source == "merged")
    combined = [*keep, *merged_fb]
    combined.sort(key=lambda p: p.timestamp_unix if p.timestamp_unix is not None else 0, reverse=True)
    return combined, newly_merged, amb


_KO_OTHERS_RE = re.compile(r"님?\s*외\s*(\d+)명$")


def _norm_key(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


def _norm_thread_title(text: str | None) -> str:
    s = " ".join((text or "").split()).strip()
    s = _KO_OTHERS_RE.sub(r" and \1 others", s)
    if s.endswith("님") and len(s) >= 2:
        s = s[:-1].strip()
    return s.lower()


def _participant_keys(participants: Sequence[UserRef]) -> set[str]:
    keys: set[str] = set()
    for p in participants:
        for val in (p.username, p.full_name, p.user_id):
            k = _norm_key(val)
            if k:
                keys.add(k)
    return keys


def _find_matching_thread(
    existing_threads: Sequence[MessageThread],
    incoming: MessageThread,
) -> int | None:
    # 1. Exact (platform, thread_id) match
    for idx, ex in enumerate(existing_threads):
        if ex.platform == incoming.platform and ex.thread_id == incoming.thread_id:
            return idx

    # 2. Match numeric suffix (e.g., official `inbox/name_12345` vs live `12345`)
    inc_tail = incoming.thread_id.rsplit("_", 1)[-1].rsplit("/", 1)[-1]
    if inc_tail.isdigit() and len(inc_tail) >= 6:
        for idx, ex in enumerate(existing_threads):
            if ex.platform != incoming.platform:
                continue
            ex_tail = ex.thread_id.rsplit("_", 1)[-1].rsplit("/", 1)[-1]
            if ex_tail == inc_tail:
                return idx

    # 3. Exact normalized title match when unique within platform
    inc_title = _norm_thread_title(incoming.title)
    if inc_title:
        title_matches = [
            idx
            for idx, ex in enumerate(existing_threads)
            if ex.platform == incoming.platform and _norm_thread_title(ex.title) == inc_title
        ]
        if len(title_matches) == 1:
            return title_matches[0]

    # 4. Exact participant name/username set match when >= 2 participants
    inc_parts = _participant_keys(incoming.participants)
    if len(inc_parts) >= 2:
        part_matches = [
            idx
            for idx, ex in enumerate(existing_threads)
            if ex.platform == incoming.platform
            and _participant_keys(ex.participants)
            and inc_parts == _participant_keys(ex.participants)
        ]
        if len(part_matches) == 1:
            return part_matches[0]

    return None


def _is_duplicate_message(existing: Sequence[Message], candidate: Message) -> bool:
    if candidate.message_id:
        for ex in existing:
            if ex.message_id and ex.message_id == candidate.message_id:
                return True

    cand_ms = _message_ts_ms(candidate)
    cand_sender = _norm_key(candidate.sender)
    cand_text = _norm_key(candidate.text)
    cand_link = (candidate.shared_link or "").strip()

    for ex in existing:
        ex_ms = _message_ts_ms(ex)
        if cand_ms and ex_ms and abs(cand_ms - ex_ms) <= 120_000:
            if cand_sender and _norm_key(ex.sender) and cand_sender != _norm_key(ex.sender):
                continue
            ex_text = _norm_key(ex.text)
            ex_link = (ex.shared_link or "").strip()
            if cand_text and cand_text == ex_text:
                return True
            if cand_link and cand_link == ex_link:
                return True
            if (
                not cand_text
                and not ex_text
                and bool(candidate.media) == bool(ex.media)
                and abs(cand_ms - ex_ms) <= 5000
            ):
                return True
    return False


def _merge_comment_lists(existing: Sequence[Comment], incoming: Sequence[Comment]) -> tuple[list[Comment], int]:
    merged: list[Comment] = [c.model_copy(deep=True) for c in existing]
    by_id = {c.comment_id: i for i, c in enumerate(merged) if c.comment_id}
    by_fallback = {
        (_norm_key(c.author.username or c.author.full_name), c.timestamp_unix or 0, _norm_key(c.text)): i
        for i, c in enumerate(merged)
    }
    added = 0
    for inc in incoming:
        idx = by_id.get(inc.comment_id) if inc.comment_id else None
        if idx is None:
            fb_key = (
                _norm_key(inc.author.username or inc.author.full_name),
                inc.timestamp_unix or 0,
                _norm_key(inc.text),
            )
            idx = by_fallback.get(fb_key)
        if idx is None:
            merged.append(inc.model_copy(deep=True))
            if inc.comment_id:
                by_id[inc.comment_id] = len(merged) - 1
            added += 1
        else:
            ex = merged[idx]
            new_replies, rep_added = _merge_comment_lists(ex.replies, inc.replies)
            ex.replies = new_replies
            if inc.like_count is not None and (ex.like_count is None or inc.like_count > ex.like_count):
                ex.like_count = inc.like_count
            added += rep_added
    return merged, added


def _merge_delta_posts(
    existing_posts: Sequence[Post],
    delta_posts: Sequence[Post],
) -> tuple[list[Post], int, int]:
    out = [p.model_copy(deep=True) for p in existing_posts]
    by_id = {(p.platform, p.post_id): i for i, p in enumerate(out)}
    by_sc = {(p.platform, p.shortcode): i for i, p in enumerate(out) if p.shortcode}
    added = 0
    updated = 0

    for inc in delta_posts:
        idx = by_id.get((inc.platform, inc.post_id))
        if idx is None and inc.shortcode:
            idx = by_sc.get((inc.platform, inc.shortcode))
        if idx is None:
            out.append(inc.model_copy(deep=True))
            by_id[(inc.platform, inc.post_id)] = len(out) - 1
            if inc.shortcode:
                by_sc[(inc.platform, inc.shortcode)] = len(out) - 1
            added += 1
        else:
            ex = out[idx]
            changed = False
            merged_cmts, cmt_added = _merge_comment_lists(ex.comments, inc.comments)
            if cmt_added > 0:
                ex.comments = merged_cmts
                changed = True
            if inc.likes.count is not None and (
                ex.likes.count is None or inc.likes.count > ex.likes.count
            ):
                ex.likes.count = inc.likes.count
                changed = True
            if inc.likes.users:
                seen_u = {
                    (u.username or "").lower()
                    for u in ex.likes.users
                    if u.username
                }
                for u in inc.likes.users:
                    if u.username and u.username.lower() not in seen_u:
                        ex.likes.users.append(u.model_copy(deep=True))
                        seen_u.add(u.username.lower())
                        changed = True
            if inc.reactions.total > ex.reactions.total:
                ex.reactions = inc.reactions.model_copy(deep=True)
                changed = True
            if changed:
                updated += 1

    out.sort(key=lambda p: p.timestamp_unix if p.timestamp_unix is not None else 0, reverse=True)
    return out, added, updated


def _merge_delta_connections(
    existing: Sequence[Connection],
    incoming: Sequence[Connection],
) -> tuple[list[Connection], int]:
    out = [c.model_copy(deep=True) for c in existing]
    seen_ig = {
        (c.relation, _norm_key(c.username))
        for c in out
        if c.platform == "instagram" and c.username
    }
    seen_fb_uid = {
        (c.relation, c.user_id)
        for c in out
        if c.platform == "facebook" and c.user_id
    }
    seen_fb_name = {
        (c.relation, _norm_key(c.full_name))
        for c in out
        if c.platform == "facebook" and c.full_name
    }

    added = 0
    for inc in incoming:
        if inc.platform == "instagram":
            key = (inc.relation, _norm_key(inc.username))
            if key[1] and key in seen_ig:
                continue
            if key[1]:
                seen_ig.add(key)
            out.append(inc.model_copy(deep=True))
            added += 1
        else:
            if inc.user_id and (inc.relation, inc.user_id) in seen_fb_uid:
                continue
            if inc.full_name and (inc.relation, _norm_key(inc.full_name)) in seen_fb_name:
                continue
            if inc.user_id:
                seen_fb_uid.add((inc.relation, inc.user_id))
            if inc.full_name:
                seen_fb_name.add((inc.relation, _norm_key(inc.full_name)))
            out.append(inc.model_copy(deep=True))
            added += 1

    return out, added


def _resolve_staged_file(local_path: str | None, roots: Sequence[Path]) -> Path | None:
    if not local_path:
        return None
    p = Path(local_path)
    if p.is_absolute() and os.path.exists(to_long_path(p)):
        return p
    norm = normalize_rel_path(local_path)
    parts = norm.split("/")
    for root in roots:
        cand = root.joinpath(*parts)
        if os.path.exists(to_long_path(cand)):
            return cand
    return None


def _backup_archive_json(archive_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = archive_dir.parent / "archive_backups" / stamp
    os.makedirs(to_long_path(backup_root), exist_ok=True)
    paths = ArchivePaths(archive_dir)
    for src in (
        paths.profiles_json,
        paths.posts_json,
        paths.stories_json,
        paths.connections_json,
        paths.messages_json,
        paths.activities_json,
        paths.ambiguous_matches_path,
    ):
        if os.path.exists(to_long_path(src)):
            shutil.copy2(to_long_path(src), to_long_path(backup_root / src.name))
    return backup_root


def apply_sync_delta(
    archive_dir: Path,
    delta: SyncDelta | None = None,
    *,
    apply: bool = False,
    dedup_fb: bool = True,
    tz_offset: str = "+09:00",
) -> PatchSummary:
    """Preview (`apply=False`) or atomically apply (`apply=True`) FB dedup & Playwright delta to an archive."""
    delta = delta or SyncDelta()
    baseline = load_archive_baseline(archive_dir)
    paths = ArchivePaths(archive_dir)
    summary = PatchSummary(applied=apply, notes=list(delta.notes))

    # 1. Profiles
    profiles = merge_profiles(baseline.profiles, delta.profiles) if delta.profiles else list(baseline.profiles)

    # 2. Posts: optional FB stub dedup + delta post/comment/like merge
    posts = [p.model_copy(deep=True) for p in baseline.posts]
    ambiguous_raw = list(baseline.ambiguous_matches)
    if dedup_fb:
        posts, merged_cnt, rem_post_amb = dedup_fb_posts_in_archive(posts, archive_root=archive_dir)
        summary.fb_stubs_merged = merged_cnt
        summary.fb_ambiguous_remaining = len(rem_post_amb)
        non_fb_post_amb = [
            a
            for a in ambiguous_raw
            if not (a.get("platform") == "facebook" and a.get("entity_type") == "post")
        ]
        ambiguous_raw = [
            *non_fb_post_amb,
            *[
                {
                    "entity_type": a.entity_type,
                    "platform": a.platform,
                    "source_id": a.source_id,
                    "candidate_ids": a.candidate_ids,
                    "reason": a.reason,
                    "details": a.details,
                }
                for a in rem_post_amb
            ],
        ]

    if delta.posts:
        posts, posts_added, posts_updated = _merge_delta_posts(posts, delta.posts)
        summary.posts_added = posts_added
        summary.posts_updated = posts_updated

    # 3. Stories
    existing_story_ids = {(s.platform, s.story_id) for s in baseline.stories}
    new_stories = [s for s in delta.stories if (s.platform, s.story_id) not in existing_story_ids]
    stories = merge_stories(
        baseline.stories,
        new_stories,
        official_roots=[archive_dir],
        playwright_roots=delta.staged_roots,
    ) if new_stories else list(baseline.stories)
    summary.stories_added = len(new_stories)

    # 4. Connections
    connections, conns_added = _merge_delta_connections(baseline.connections, delta.connections)
    summary.connections_added = conns_added

    # 5. Message threads (IG DM + FB Messenger incremental)
    threads = [t.model_copy(deep=True) for t in baseline.messages]
    for inc_thread in delta.message_threads:
        match_idx = _find_matching_thread(threads, inc_thread)
        if match_idx is None:
            new_msgs = sorted(inc_thread.messages, key=_message_ts_ms)
            if not new_msgs:
                continue
            cloned = inc_thread.model_copy(deep=True)
            cloned.messages = new_msgs
            threads.append(cloned)
            if inc_thread.platform == "instagram":
                summary.ig_new_threads += 1
                summary.ig_new_messages += len(new_msgs)
            else:
                summary.fb_new_threads += 1
                summary.fb_new_messages += len(new_msgs)
            summary.thread_previews.append(
                {
                    "platform": inc_thread.platform,
                    "action": "new_thread",
                    "thread_id": cloned.thread_id,
                    "title": cloned.title or cloned.thread_id,
                    "is_e2ee": cloned.is_e2ee,
                    "added_messages": len(new_msgs),
                    "latest_at": new_msgs[-1].created_at.isoformat() if new_msgs[-1].created_at else None,
                    "sample_text": (new_msgs[-1].text or "")[:60],
                }
            )
        else:
            ex = threads[match_idx]
            added_msgs: list[Message] = []
            for cand in inc_thread.messages:
                if not _is_duplicate_message(ex.messages, cand):
                    added_msgs.append(cand.model_copy(deep=True))
            if added_msgs:
                ex.messages.extend(added_msgs)
                ex.messages.sort(key=_message_ts_ms)
                if inc_thread.is_e2ee:
                    ex.is_e2ee = True
                if inc_thread.platform == "instagram":
                    summary.ig_updated_threads += 1
                    summary.ig_new_messages += len(added_msgs)
                else:
                    summary.fb_updated_threads += 1
                    summary.fb_new_messages += len(added_msgs)
                summary.thread_previews.append(
                    {
                        "platform": inc_thread.platform,
                        "action": "appended",
                        "thread_id": ex.thread_id,
                        "title": ex.title or ex.thread_id,
                        "is_e2ee": ex.is_e2ee,
                        "added_messages": len(added_msgs),
                        "latest_at": added_msgs[-1].created_at.isoformat() if added_msgs[-1].created_at else None,
                        "sample_text": (added_msgs[-1].text or "")[:60],
                    }
                )

    # 6. DM missing media repairs
    thread_by_id = {t.thread_id: t for t in threads}
    valid_dm_repairs: list[tuple[DmMediaRepair, MessageThread, Message, MediaItem]] = []
    for rep in delta.dm_media_repairs:
        t = thread_by_id.get(rep.thread_id)
        if t is None or rep.message_index >= len(t.messages):
            continue
        msg = t.messages[rep.message_index]
        if rep.media_index >= len(msg.media):
            continue
        if os.path.exists(to_long_path(rep.staged_file)):
            valid_dm_repairs.append((rep, t, msg, msg.media[rep.media_index]))
    summary.dm_media_repaired = len(valid_dm_repairs)

    # 7. Silent video audio repairs / confirmations
    post_by_id = {p.post_id: p for p in posts}
    for vrep in delta.video_audio_repairs:
        summary.video_outcomes.append(
            {
                "post_id": vrep.post_id,
                "shortcode": vrep.shortcode,
                "media_index": vrep.media_index,
                "status": vrep.status,
                "detail": vrep.detail,
            }
        )
        if vrep.status == "repaired" and vrep.staged_file and os.path.exists(to_long_path(vrep.staged_file)):
            summary.videos_audio_repaired += 1
        elif vrep.status == "confirmed_silent":
            summary.videos_confirmed_silent += 1

    if not apply:
        return summary

    # --- APPLY MODE ---
    summary.backup_dir = _backup_archive_json(archive_dir)
    roots = [archive_dir, *delta.staged_roots]

    def _materialize_staged(
        src_file: Path,
        platform: str,
        category: str,
        stem_hint: str,
        *,
        ts_unix: int | None,
        description: str | None = None,
        write_xmp: bool = True,
    ) -> tuple[str, str | None, str, int]:
        ext = _sniff_normalized_ext(src_file, src_file.suffix or ".jpg")
        dest_rel = ArchivePaths.media_rel_path(platform, category, f"{stem_hint}{ext}")
        dest_file = paths.root.joinpath(*dest_rel.split("/"))
        src_sha = sha256_file(src_file)
        if os.path.exists(to_long_path(dest_file)):
            dest_sha = sha256_file(dest_file)
            if dest_sha == src_sha:
                sha = dest_sha
                sz = os.path.getsize(to_long_path(dest_file))
            else:
                dest_rel = ArchivePaths.media_rel_path(
                    platform, category, f"{stem_hint}_{src_sha[:8]}{ext}"
                )
                dest_file = paths.root.joinpath(*dest_rel.split("/"))
                sha, sz = _copy_file_lossless(src_file, dest_file)
                summary.media_copied += 1
        else:
            sha, sz = _copy_file_lossless(src_file, dest_file)
            summary.media_copied += 1

        xmp_rel: str | None = None
        if write_xmp and ts_unix is not None:
            sidecar = write_xmp_sidecar(
                dest_file,
                timestamp_unix=ts_unix,
                tz_offset=tz_offset,
                description=description,
            )
            if sidecar is not None:
                xmp_rel = normalize_rel_path(sidecar.relative_to(paths.root).as_posix())
                summary.xmp_written += 1
        if ts_unix is not None:
            set_file_mtime(dest_file, ts_unix)
        return dest_rel, xmp_rel, sha, sz

    # Materialize DM missing media repairs
    for rep, t, msg, med in valid_dm_repairs:
        safe_tid = rep.thread_id.replace("/", "_").replace("\\", "_")[-40:]
        stem = f"repaired_{safe_tid}_{rep.message_index}_{rep.media_index}"
        dest_rel, xmp_rel, sha, sz = _materialize_staged(
            rep.staged_file,
            t.platform,
            "messages",
            stem,
            ts_unix=msg.timestamp_unix,
            write_xmp=True,
        )
        med.local_path = dest_rel
        med.xmp_path = xmp_rel or med.xmp_path
        med.sha256 = sha
        med.byte_size = sz
        if rep.width:
            med.width = rep.width
        if rep.height:
            med.height = rep.height

    # Materialize repaired/confirmed silent post videos
    for vrep in delta.video_audio_repairs:
        post = post_by_id.get(vrep.post_id)
        if post is None or vrep.media_index >= len(post.media):
            continue
        med = post.media[vrep.media_index]
        if vrep.status == "repaired" and vrep.staged_file and os.path.exists(to_long_path(vrep.staged_file)):
            if med.local_path:
                dest_file = paths.root.joinpath(*normalize_rel_path(med.local_path).split("/"))
                sha, sz = _copy_file_lossless(vrep.staged_file, dest_file)
                summary.media_copied += 1
                if post.timestamp_unix is not None:
                    set_file_mtime(dest_file, post.timestamp_unix)
                mp4_res = inspect_mp4(dest_file)
                med.sha256 = sha
                med.byte_size = sz
                med.has_audio = mp4_res.has_audio if mp4_res.ok else True
                med.is_audio_muted = False
        elif vrep.status == "confirmed_silent":
            med.is_audio_muted = True

    # Materialize any staged media inside newly added posts, stories, messages, connections
    for post in posts:
        for idx, med in enumerate(post.media):
            if not med.local_path:
                continue
            arch_file = paths.root.joinpath(*normalize_rel_path(med.local_path).split("/"))
            if os.path.exists(to_long_path(arch_file)):
                continue
            staged = _resolve_staged_file(med.local_path, roots)
            if staged is not None:
                stem = Path(med.local_path).stem or f"{post.post_id}_{idx:02d}"
                dest_rel, xmp_rel, sha, sz = _materialize_staged(
                    staged,
                    post.platform,
                    "posts",
                    stem,
                    ts_unix=med.taken_at_unix or post.timestamp_unix,
                    description=post.caption or None,
                    write_xmp=True,
                )
                med.local_path = dest_rel
                med.xmp_path = xmp_rel
                med.sha256 = sha
                med.byte_size = sz

    for story in stories:
        if story.media and story.media.local_path:
            arch_file = paths.root.joinpath(*normalize_rel_path(story.media.local_path).split("/"))
            if not os.path.exists(to_long_path(arch_file)):
                staged = _resolve_staged_file(story.media.local_path, roots)
                if staged is not None:
                    stem = Path(story.media.local_path).stem or story.story_id
                    dest_rel, xmp_rel, sha, sz = _materialize_staged(
                        staged,
                        story.platform,
                        "stories",
                        stem,
                        ts_unix=story.timestamp_unix,
                        description=story.caption or story.highlight_title or None,
                        write_xmp=True,
                    )
                    story.media.local_path = dest_rel
                    story.media.xmp_path = xmp_rel
                    story.media.sha256 = sha
                    story.media.byte_size = sz

    for t in threads:
        for mi, msg in enumerate(t.messages):
            for med_i, med in enumerate(msg.media):
                if not med.local_path:
                    continue
                arch_file = paths.root.joinpath(*normalize_rel_path(med.local_path).split("/"))
                if os.path.exists(to_long_path(arch_file)):
                    continue
                staged = _resolve_staged_file(med.local_path, roots)
                if staged is not None:
                    stem = Path(med.local_path).stem or f"sync_{mi}_{med_i}"
                    dest_rel, xmp_rel, sha, sz = _materialize_staged(
                        staged,
                        t.platform,
                        "messages",
                        stem,
                        ts_unix=msg.timestamp_unix,
                        write_xmp=True,
                    )
                    med.local_path = dest_rel
                    med.xmp_path = xmp_rel
                    med.sha256 = sha
                    med.byte_size = sz

    for conn in connections:
        if conn.avatar_local_path:
            arch_file = paths.root.joinpath(*normalize_rel_path(conn.avatar_local_path).split("/"))
            if not os.path.exists(to_long_path(arch_file)):
                staged = _resolve_staged_file(conn.avatar_local_path, roots)
                if staged is not None:
                    stem = Path(conn.avatar_local_path).stem or (conn.username or conn.user_id or "avatar")
                    dest_rel, _, _, _ = _materialize_staged(
                        staged,
                        conn.platform,
                        "avatars",
                        stem,
                        ts_unix=None,
                        write_xmp=False,
                    )
                    conn.avatar_local_path = dest_rel

    def _dump_json(path: Path, data: Any) -> None:
        atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    _dump_json(paths.profiles_json, [p.model_dump(mode="json") for p in profiles])
    _dump_json(paths.posts_json, [p.model_dump(mode="json") for p in posts])
    _dump_json(paths.stories_json, [s.model_dump(mode="json") for s in stories])
    _dump_json(paths.connections_json, [c.model_dump(mode="json") for c in connections])
    _dump_json(paths.messages_json, [t.model_dump(mode="json") for t in threads])
    _dump_json(paths.activities_json, [a.model_dump(mode="json") for a in baseline.activities])
    _dump_json(paths.ambiguous_matches_path, ambiguous_raw)

    build_sqlite_index(archive_dir)
    write_manifest_sha256(archive_dir)
    checked, failures = verify_manifest_sha256(archive_dir)
    summary.manifest_verified = checked
    summary.manifest_failures = failures
    return summary
