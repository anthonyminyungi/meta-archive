"""Canonical Pydantic v2 schemas for unified Instagram and Facebook archives."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from meta_archive_pipeline.schema.paths import normalize_rel_path

Platform = Literal["instagram", "facebook"]


def _normalize_optional_rel_path(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_rel_path(value)


class UserRef(BaseModel):
    """Reference to an account or person appearing in posts, comments, or threads."""

    user_id: str | None = None
    username: str | None = None
    full_name: str | None = None
    profile_url: str | None = None
    avatar_local_path: str | None = None

    @field_validator("avatar_local_path")
    @classmethod
    def _validate_avatar_path(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class Location(BaseModel):
    """Geographic location metadata attached to a post or story."""

    location_id: str | None = None
    name: str | None = None
    description: str | None = None
    address: str | None = None
    lat: float | None = None
    lng: float | None = None


class MediaItem(BaseModel):
    """Single image, video, or audio asset stored in the archive."""

    media_id: str | None = None
    media_type: Literal["image", "video", "audio", "unknown"] = "unknown"
    local_path: str | None = None
    xmp_path: str | None = None
    original_uri: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    has_audio: bool | None = None
    is_audio_muted: bool | None = None
    taken_at_exif: str | None = None
    taken_at_unix: int | None = None
    gps_lat: float | None = None
    gps_lng: float | None = None
    sha256: str | None = None
    byte_size: int | None = None
    is_carousel_cover: bool = False

    @field_validator("local_path", "xmp_path")
    @classmethod
    def _validate_rel_paths(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class ReactionSummary(BaseModel):
    """Aggregated reactions (primarily Facebook reaction types)."""

    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)


class LikesInfo(BaseModel):
    """Post like count and optionally captured liking user list."""

    count: int | None = None
    is_truncated: bool = False
    users: list[UserRef] = Field(default_factory=list)


class Comment(BaseModel):
    """Post or reel comment with recursive replies."""

    comment_id: str | None = None
    author: UserRef = Field(default_factory=UserRef)
    text: str = ""
    created_at: datetime | None = None
    timestamp_unix: int | None = None
    like_count: int | None = None
    media: list[MediaItem] = Field(default_factory=list)
    stickers: list[str] = Field(default_factory=list)
    reactions: ReactionSummary | None = None
    replies: list[Comment] = Field(default_factory=list)


Comment.model_rebuild()


class ProfileCounts(BaseModel):
    """Header/summary counts captured for an account profile."""

    posts: int | None = None
    followers: int | None = None
    following: int | None = None
    friends: int | None = None


class Profile(BaseModel):
    """Archived account profile metadata."""

    platform: Platform
    user_id: str | None = None
    username: str | None = None
    full_name: str | None = None
    bio: str | None = None
    avatar_local_path: str | None = None
    counts: ProfileCounts = Field(default_factory=ProfileCounts)
    exported_at: datetime | None = None

    @field_validator("avatar_local_path")
    @classmethod
    def _validate_avatar_path(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class SharedPostRef(BaseModel):
    """Embedded reference when a post shares another post or external link."""

    url: str | None = None
    author_name: str | None = None
    text: str | None = None
    title: str | None = None
    domain: str | None = None
    thumbnail_local_path: str | None = None

    @field_validator("thumbnail_local_path")
    @classmethod
    def _validate_thumbnail_path(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class Post(BaseModel):
    """Unified Instagram/Facebook feed post, carousel, reel, album photo, group post, or tagged post."""

    platform: Platform
    post_id: str
    shortcode: str | None = None
    url: str | None = None
    post_type: Literal[
        "post", "carousel", "reel", "album_photo", "shared", "group_post", "tagged", "other"
    ] = "post"
    author: UserRef = Field(default_factory=UserRef)
    created_at: datetime | None = None
    timestamp_unix: int | None = None
    caption: str = ""
    activity_title: str | None = None
    group_name: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    location: Location | None = None
    tagged_users: list[UserRef] = Field(default_factory=list)
    media: list[MediaItem] = Field(default_factory=list)
    likes: LikesInfo = Field(default_factory=LikesInfo)
    reactions: ReactionSummary = Field(default_factory=ReactionSummary)
    comments: list[Comment] = Field(default_factory=list)
    album_name: str | None = None
    shared_post: SharedPostRef | None = None
    audience: str | None = None
    is_archived: bool = False
    is_collab: bool = False
    source: Literal["playwright", "official_export", "merged"] = "official_export"


class Story(BaseModel):
    """Archived story or highlight item (media may be None for empty-URI export entries)."""

    platform: Platform
    story_id: str
    created_at: datetime | None = None
    timestamp_unix: int | None = None
    caption: str = ""
    source: Literal["highlight_web", "official_export", "merged"] = "official_export"
    highlight_id: str | None = None
    highlight_title: str | None = None
    highlight_cover: str | None = None
    media: MediaItem | None = None
    location: Location | None = None
    stickers: list[str] = Field(default_factory=list)
    likes_count: int | None = None

    @field_validator("highlight_cover")
    @classmethod
    def _validate_highlight_cover(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class Connection(BaseModel):
    """Follower, following, or friend relationship entry."""

    platform: Platform
    relation: Literal["follower", "following", "friend"]
    user_id: str | None = None
    username: str | None = None
    full_name: str | None = None
    profile_url: str | None = None
    avatar_local_path: str | None = None
    connected_at: datetime | None = None
    timestamp_unix: int | None = None
    match_confidence: Literal["exact", "heuristic", "unmatched"] = "exact"

    @field_validator("avatar_local_path")
    @classmethod
    def _validate_avatar_path(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


class MessageReaction(BaseModel):
    """Reaction emoji attached to a direct message."""

    actor: str
    reaction: str


class Message(BaseModel):
    """Single message within an Instagram DM or Facebook Messenger thread."""

    message_id: str | None = None
    sender: str
    timestamp_unix: int | None = None
    timestamp_ms: int | None = None
    created_at: datetime | None = None
    text: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    reactions: list[MessageReaction] = Field(default_factory=list)
    reply_to_id: str | None = None
    shared_link: str | None = None
    share_text: str | None = None
    share_owner: str | None = None
    is_unsent: bool = False
    is_missed_call: bool = False
    call_duration: int | None = None


class MessageThread(BaseModel):
    """Conversation thread containing participants and messages."""

    platform: Platform
    thread_id: str
    title: str | None = None
    avatar_local_path: str | None = None
    participants: list[UserRef] = Field(default_factory=list)
    is_e2ee: bool = False
    is_pending: bool = False
    messages: list[Message] = Field(default_factory=list)

    @field_validator("avatar_local_path")
    @classmethod
    def _validate_avatar_path(cls, value: str | None) -> str | None:
        return _normalize_optional_rel_path(value)


ActivityKind = Literal["own_comment", "group_comment", "saved_item", "repost"]


class ActivityItem(BaseModel):
    """User's own comment on external/group posts, saved item/bookmark, or repost."""

    activity_id: str
    platform: Platform
    kind: ActivityKind
    created_at: datetime | None = None
    timestamp_unix: int | None = None
    text: str = ""
    title: str | None = None
    target_author: str | None = None
    group_name: str | None = None
    url: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    source: Literal["official_export", "playwright", "merged"] = "official_export"
