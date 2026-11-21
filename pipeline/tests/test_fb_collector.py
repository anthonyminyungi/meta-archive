"""Tests for Facebook Comet GraphQL parsers, All-Comments switching, and CDP collector."""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

from PIL import Image

from meta_archive_pipeline.collect.cdp import CooldownTracker
from meta_archive_pipeline.collect.fb_collector import (
    FbCollectConfig,
    FbCollector,
    extract_fb_friends_from_payloads,
    extract_fb_posts_from_payloads,
    switch_to_all_comments,
)
from meta_archive_pipeline.utils.checkpoint import CheckpointStore


def _jpeg_bytes(color: tuple[int, int, int] = (50, 120, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def test_extract_fb_friends_dedup_and_avatars() -> None:
    payloads = [
        {
            "data": {
                "node": {
                    "all_friends": {
                        "edges": [
                            {
                                "node": {
                                    "id": "fb_u1",
                                    "name": "홍길동",
                                    "url": "https://www.facebook.com/hong",
                                    "profile_picture": {"uri": "https://cdn.fb.com/u1.jpg"},
                                }
                            },
                            {
                                "node": {
                                    "id": "fb_u1",
                                    "name": "홍길동",
                                    "url": "https://www.facebook.com/hong",
                                }
                            },
                        ]
                    },
                    "pageItems": {
                        "edges": [
                            {
                                "node": {
                                    "id": "fb_u2",
                                    "title": {"text": "김영희"},
                                    "url": "https://www.facebook.com/kim",
                                    "image": {"uri": "https://cdn.fb.com/u2.jpg"},
                                }
                            }
                        ]
                    },
                }
            }
        }
    ]

    friends, avatars = extract_fb_friends_from_payloads(payloads)
    assert len(friends) == 2
    assert friends[0].relation == "friend"
    assert friends[0].full_name == "홍길동"
    assert friends[1].full_name == "김영희"
    assert avatars == {
        "fb_u1": "https://cdn.fb.com/u1.jpg",
        "fb_u2": "https://cdn.fb.com/u2.jpg",
    }


def test_extract_fb_posts_reactions_comments_attachments_and_dash() -> None:
    dash_xml = """
    <MPD>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation bandwidth="1500000"><BaseURL>https://cdn.fb.com/hd.mp4</BaseURL></Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="audio/mp4">
          <Representation bandwidth="128000"><BaseURL>https://cdn.fb.com/audio.m4a</BaseURL></Representation>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    payload = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "fb_post_1",
                "permalink_url": "https://www.facebook.com/me/posts/fb_post_1",
                "creation_time": 1705000000,
                "actors": [{"id": "me_1", "name": "윤기", "url": "https://www.facebook.com/me"}],
                "message": {"text": "부산 바다 기록 #부산 #여행"},
                "privacy_scope": {"label": "친구만"},
                "attachments": [
                    {
                        "media": {
                            "__typename": "Photo",
                            "id": "ph_1",
                            "album": {"name": "2024 부산"},
                            "viewer_image": {"uri": "https://cdn.fb.com/p1.jpg", "width": 1080, "height": 720},
                        }
                    },
                    {
                        "media": {
                            "__typename": "Video",
                            "id": "vid_1",
                            "playable_url_quality_hd": "https://cdn.fb.com/vid1_hd.mp4",
                            "dash_manifest": dash_xml,
                        }
                    },
                ],
                "attached_story": {
                    "permalink_url": "https://www.facebook.com/orig/123",
                    "actors": [{"name": "원작자"}],
                    "message": {"text": "원본 글"},
                },
                "feedback": {
                    "reaction_count": {"count": 15},
                    "top_reactions": {
                        "edges": [
                            {"reaction_count": 10, "node": {"reaction_type": "LIKE"}},
                            {"reaction_count": 3, "node": {"reaction_type": "LOVE"}},
                            {"reaction_count": 2, "node": {"reaction_type": "HAHA"}},
                        ]
                    },
                    "display_comments": {
                        "edges": [
                            {
                                "node": {
                                    "id": "fc_1",
                                    "created_time": 1705000100,
                                    "author": {"id": "u2", "name": "친구A"},
                                    "body": {"text": "재밌겠다!"},
                                    "attachments": [
                                        {"media": {"image": {"uri": "https://cdn.fb.com/c_photo.jpg"}}}
                                    ],
                                    "feedback": {
                                        "reaction_count": {"count": 4},
                                        "display_comments": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "id": "fc_reply_1",
                                                        "created_time": 1705000200,
                                                        "author": {"id": "me_1", "name": "윤기"},
                                                        "body": {"text": "다음에 같이 가자"},
                                                    }
                                                }
                                            ]
                                        },
                                    },
                                }
                            }
                        ]
                    },
                },
            }
        }
    }

    posts, candidates = extract_fb_posts_from_payloads([payload])
    assert len(posts) == 1
    post = posts[0]
    assert post.post_id == "fb_post_1"
    assert post.post_type == "album_photo"
    assert post.album_name == "2024 부산"
    assert post.audience == "친구만"
    assert post.hashtags == ["부산", "여행"]
    assert post.shared_post is not None and post.shared_post.author_name == "원작자"
    assert post.reactions.total == 15
    assert post.reactions.by_type == {"LIKE": 10, "LOVE": 3, "HAHA": 2}
    assert len(post.comments) == 1
    assert post.comments[0].media[0].original_uri == "https://cdn.fb.com/c_photo.jpg"
    assert post.comments[0].like_count == 4
    assert [r.comment_id for r in post.comments[0].replies] == ["fc_reply_1"]

    assert len(candidates) == 2
    assert candidates[0].media_type == "image"
    assert candidates[0].progressive_url == "https://cdn.fb.com/p1.jpg"
    assert candidates[1].media_type == "video"
    assert candidates[1].dash_video_url == "https://cdn.fb.com/hd.mp4"
    assert candidates[1].dash_audio_url == "https://cdn.fb.com/audio.m4a"


class _FakeLocator:
    def __init__(self, page: _FakeFbPage, label: str) -> None:
        self._page = page
        self._label = label

    def count(self) -> int:
        return 1 if self._page.visible_counts.get(self._label, 0) > 0 else 0

    def nth(self, idx: int) -> _FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._page.visible_counts.get(self._label, 0) > 0

    def click(self) -> None:
        self._page.visible_counts[self._label] -= 1
        self._page.clicked.append(self._label)
        # Clicking the sort trigger reveals the "모든 댓글" / "All comments" menu option
        if self._label == "가장 관련성 높은 댓글":
            self._page.visible_counts["모든 댓글"] = 1
        elif self._label == "Most relevant":
            self._page.visible_counts["All comments"] = 1


class _FakeResp:
    def __init__(self, url: str, status: int, body: bytes | str) -> None:
        self.url = url
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    def text(self) -> str:
        return self._body if isinstance(self._body, str) else ""

    def body(self) -> bytes:
        return self._body if isinstance(self._body, bytes) else self._body.encode("utf-8")


class _FakeRequestContext:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
        if url in self._blobs:
            return _FakeResp(url, 200, self._blobs[url])
        return _FakeResp(url, 404, b"")


class _FakeFbPage:
    def __init__(self, routes: dict[str, tuple[str, list[_FakeResp]]], visible_counts: dict[str, int] | None = None) -> None:
        self._routes = routes
        self._handlers: dict[str, list] = {}
        self.url = ""
        self._html = ""
        self.clicked: list[str] = []
        self.visible_counts = dict(visible_counts or {})

    def on(self, event: str, fn) -> None:
        self._handlers.setdefault(event, []).append(fn)

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.url = url
        html, resps = self._routes[url]
        self._html = html
        for r in resps:
            for fn in self._handlers.get("response", []):
                fn(r)

    def content(self) -> str:
        return self._html

    def inner_text(self, selector: str) -> str:
        return ""

    def evaluate(self, script: str):
        return 1000

    def get_by_role(self, role: str, name: str) -> _FakeLocator:
        return _FakeLocator(self, name)

    def get_by_text(self, text: str) -> _FakeLocator:
        return _FakeLocator(self, text)


def test_switch_to_all_comments_ko_and_en() -> None:
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)

    page_ko = _FakeFbPage({}, visible_counts={"가장 관련성 높은 댓글": 1})
    assert switch_to_all_comments(page_ko, cooldown) == 1
    assert page_ko.clicked == ["가장 관련성 높은 댓글", "모든 댓글"]

    page_en = _FakeFbPage({}, visible_counts={"Most relevant": 1})
    assert switch_to_all_comments(page_en, cooldown) == 1
    assert page_en.clicked == ["Most relevant", "All comments"]


def test_fb_collector_posts_friends_and_safety_pause(tmp_path: Path) -> None:
    blobs = {
        "https://cdn.fb.com/photo1.jpg": _jpeg_bytes((20, 80, 140)),
        "https://cdn.fb.com/avatar1.jpg": _jpeg_bytes((200, 100, 50)),
    }
    ndjson_posts = "\n".join(
        [
            'for (;;);{"data":{"node":{"__typename":"Story","post_id":"p100","creation_time":1710000000,"message":{"text":"테스트 글"},"attachments":[{"media":{"image":{"uri":"https://cdn.fb.com/photo1.jpg"}}}],"feedback":{"reaction_count":{"count":3}}}}}',
        ]
    )
    friends_json = json.dumps(
        {
            "data": {
                "all_friends": {
                    "edges": [
                        {
                            "node": {
                                "id": "u_friend_1",
                                "name": "친구1",
                                "url": "https://www.facebook.com/friend1",
                                "profile_picture": {"uri": "https://cdn.fb.com/avatar1.jpg"},
                            }
                        }
                    ]
                }
            }
        }
    )

    routes = {
        "https://www.facebook.com/me": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 200, ndjson_posts)],
        ),
        "https://www.facebook.com/me/friends": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 200, friends_json)],
        ),
    }

    out_dir = tmp_path / "fb_out"
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None, rng=random.Random(0))
    req_ctx = _FakeRequestContext(blobs)

    page_posts = _FakeFbPage(routes, visible_counts={"가장 관련성 높은 댓글": 1, "댓글 더 보기": 1})
    sum_posts = FbCollector(
        page_posts,
        FbCollectConfig(output_dir=out_dir, collect_pass="posts", cooldown=cooldown, max_scrolls=4),
        request_context=req_ctx,
    ).run_pass_posts()

    assert sum_posts.items_saved == 1
    assert sum_posts.media_saved == 1
    assert sum_posts.paused_reason is None
    assert page_posts.clicked == ["가장 관련성 높은 댓글", "모든 댓글", "댓글 더 보기"]
    assert (out_dir / "raw" / "posts" / "batch_0001.json").exists()
    assert (out_dir / "posts" / "p100.json").exists()
    assert (out_dir / "media" / "posts" / "p100_01.jpg").exists()
    assert CheckpointStore(out_dir / "state_fb.json").get_status("p100") == "done"

    page_friends = _FakeFbPage(routes)
    sum_friends = FbCollector(
        page_friends,
        FbCollectConfig(output_dir=out_dir, collect_pass="friends", cooldown=cooldown, max_scrolls=4),
        request_context=req_ctx,
    ).run_pass_friends()
    assert sum_friends.items_saved == 1
    assert sum_friends.media_saved == 1
    assert (out_dir / "friends.json").exists()
    assert (out_dir / "media" / "avatars" / "u_friend_1.jpg").exists()

    # Safety stop on 429 during timeline fetch
    blocked_routes = {
        "https://www.facebook.com/me": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 429, "rate limited")],
        )
    }
    sum_blocked = FbCollector(
        _FakeFbPage(blocked_routes),
        FbCollectConfig(output_dir=tmp_path / "fb_blocked", collect_pass="posts", cooldown=cooldown),
        request_context=req_ctx,
    ).run_pass_posts()
    assert sum_blocked.paused_reason is not None and "rate_limited" in sum_blocked.paused_reason
