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


def test_fb_external_link_card_extraction_and_thumbnail(tmp_path: Path) -> None:
    link_payload = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "fb_link_1",
                "creation_time": 1712000000,
                "message": {"text": "흥미로운 기술 글"},
                "attachments": [
                    {
                        "styles": {
                            "attachment": {
                                "url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fnews.example.com%2Ftech%2F1%3Ffbclid%3Dabc&h=xyz",
                                "title_with_entities": {"text": "새로운 아키텍처 정리"},
                                "description": {"text": "장기간 아카이브 구축 팁"},
                                "source": {"text": "news.example.com"},
                                "media": {
                                    "image": {"uri": "https://cdn.fb.com/link_thumb.jpg"}
                                },
                            }
                        }
                    }
                ],
                "feedback": {"reaction_count": {"count": 2}},
            }
        }
    }

    posts, cands = extract_fb_posts_from_payloads([link_payload])
    assert len(posts) == 1
    p = posts[0]
    assert p.post_type == "shared"
    assert p.media == []
    assert p.shared_post is not None
    assert p.shared_post.url == "https://news.example.com/tech/1?fbclid=abc"
    assert p.shared_post.title == "새로운 아키텍처 정리"
    assert p.shared_post.text == "장기간 아카이브 구축 팁"
    assert p.shared_post.domain == "news.example.com"
    assert len(cands) == 1
    assert cands[0].is_link_thumbnail is True

    # End-to-end collector downloads thumbnail to media/links/ without adding to post.media
    routes = {
        "https://www.facebook.com/me": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(link_payload))],
        )
    }
    blobs = {"https://cdn.fb.com/link_thumb.jpg": _jpeg_bytes((90, 160, 200))}
    out_dir = tmp_path / "fb_link_out"
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)
    summary = FbCollector(
        _FakeFbPage(routes),
        FbCollectConfig(output_dir=out_dir, collect_pass="posts", cooldown=cooldown),
        request_context=_FakeRequestContext(blobs),
    ).run_pass_posts()

    assert summary.items_saved == 1
    assert summary.media_saved == 1
    assert (out_dir / "media" / "links" / "fb_link_1_thumb.jpg").exists()
    saved = json.loads((out_dir / "posts" / "fb_link_1.json").read_text(encoding="utf-8"))
    assert saved["post_type"] == "shared"
    assert saved["media"] == []
    assert saved["shared_post"]["thumbnail_local_path"] == "media/facebook/links/fb_link_1_thumb.jpg"


def test_fb_collector_incremental_scroll_checkpoint_on_mid_429_profile_and_albums(tmp_path: Path) -> None:
    cfg_default = FbCollectConfig(output_dir=tmp_path / "cfg_check")
    assert cfg_default.max_stagnant_scrolls >= 5

    blobs = {
        "https://cdn.fb.com/me_avatar.jpg": _jpeg_bytes((15, 25, 35)),
        "https://cdn.fb.com/early.jpg": _jpeg_bytes((60, 70, 80)),
        "https://cdn.fb.com/alb_p1.jpg": _jpeg_bytes((90, 100, 110)),
    }

    early_story = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "p_early",
                "creation_time": 1710000000,
                "actors": [
                    {
                        "id": "100001",
                        "name": "윤기",
                        "url": "https://www.facebook.com/dbstnsdl12",
                        "profile_picture": {"uri": "https://cdn.fb.com/me_avatar.jpg"},
                    }
                ],
                "message": {"text": "초기 로드 게시물"},
                "attachments": [{"media": {"image": {"uri": "https://cdn.fb.com/early.jpg"}}}],
            }
        }
    }
    early_comment_delta = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "p_early",
                "feedback": {
                    "display_comments": {
                        "edges": [
                            {
                                "node": {
                                    "id": "c_early_1",
                                    "created_time": 1710000050,
                                    "author": {"name": "친구"},
                                    "body": {"text": "스크롤 중 확장된 댓글"},
                                }
                            }
                        ]
                    }
                },
            }
        }
    }

    class _DynamicScrollFbPage(_FakeFbPage):
        def __init__(self) -> None:
            super().__init__(
                {
                    "https://www.facebook.com/me": (
                        "<html></html>",
                        [_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(early_story))],
                    )
                },
                visible_counts={"댓글 더 보기": 1},
            )
            self.scroll_calls = 0

        def evaluate(self, script: str):
            if "scrollTo" in script:
                self.scroll_calls += 1
                if self.scroll_calls >= 2:
                    # Simulate mid-scroll 429 AFTER first post was already scrolled & checkpointed!
                    for fn in self._handlers.get("response", []):
                        fn(_FakeResp("https://www.facebook.com/api/graphql/", 429, "rate limited"))
                return None
            return 1000 + self.scroll_calls * 500

    dyn_page = _DynamicScrollFbPage()
    orig_click_locator = _FakeLocator.click

    def _emit_comment_on_expand(loc_self: _FakeLocator) -> None:
        orig_click_locator(loc_self)
        if loc_self._page is dyn_page and loc_self._label == "댓글 더 보기":
            for fn in dyn_page._handlers.get("response", []):
                fn(_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(early_comment_delta)))

    _FakeLocator.click = _emit_comment_on_expand  # type: ignore[method-assign]
    try:
        out_posts = tmp_path / "fb_inc_out"
        cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)
        summary = FbCollector(
            dyn_page,
            FbCollectConfig(output_dir=out_posts, collect_pass="posts", cooldown=cooldown, max_scrolls=10),
            request_context=_FakeRequestContext(blobs),
        ).run_pass_posts()
    finally:
        _FakeLocator.click = orig_click_locator  # type: ignore[method-assign]

    assert summary.paused_reason is not None and "rate_limited" in summary.paused_reason
    # Even though scroll #2 hit 429, p_early MUST already be saved with its expanded comment AND profile.json!
    assert summary.items_saved == 1
    assert (out_posts / "profile.json").exists()
    prof = json.loads((out_posts / "profile.json").read_text(encoding="utf-8"))
    assert prof["full_name"] == "윤기"
    assert prof["username"] == "dbstnsdl12"
    saved_early = json.loads((out_posts / "posts" / "p_early.json").read_text(encoding="utf-8"))
    assert [c["text"] for c in saved_early["comments"]] == ["스크롤 중 확장된 댓글"]
    assert CheckpointStore(out_posts / "state_fb.json").get_status("p_early") == "done"

    # Test albums pass traversing discovered album set URLs
    album_index_html = (
        '<html><body><a href="https://www.facebook.com/media/set/?set=a.555&amp;type=3">여행 앨범</a></body></html>'
    )
    album_story = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "fb_alb_ph_1",
                "creation_time": 1700000000,
                "attachments": [
                    {
                        "media": {
                            "__typename": "Photo",
                            "id": "ph_alb_1",
                            "album": {"name": "여행 앨범"},
                            "viewer_image": {"uri": "https://cdn.fb.com/alb_p1.jpg"},
                        }
                    }
                ],
            }
        }
    }
    album_routes = {
        "https://www.facebook.com/me/photos_albums": (album_index_html, []),
        "https://www.facebook.com/media/set/?set=a.555&type=3": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(album_story))],
        ),
    }
    out_albums = tmp_path / "fb_alb_out"
    alb_summary = FbCollector(
        _FakeFbPage(album_routes),
        FbCollectConfig(output_dir=out_albums, collect_pass="albums", cooldown=cooldown, max_scrolls=2),
        request_context=_FakeRequestContext(blobs),
    ).run_pass_albums()
    assert alb_summary.items_saved == 1
    assert (out_albums / "posts" / "fb_alb_ph_1.json").exists()


def test_fb_posts_do_not_duplicate_attached_story_or_include_sponsored_ads() -> None:
    payload = {
        "data": {
            "feed_edges": [
                {
                    "node": {
                        "__typename": "Story",
                        "post_id": "my_share_1",
                        "creation_time": 1720000000,
                        "actors": [{"id": "me_1", "name": "민윤기"}],
                        "message": {"text": "이 기사 좋네"},
                        "attached_story": {
                            "__typename": "Story",
                            "id": "foreign_orig_999",
                            "post_id": "foreign_orig_999",
                            "creation_time": 1719990000,
                            "permalink_url": "https://www.facebook.com/news/posts/999",
                            "actors": [{"id": "page_99", "name": "뉴스페이지"}],
                            "message": {"text": "원본 뉴스 글"},
                        },
                    }
                },
                {
                    "node": {
                        "__typename": "Story",
                        "post_id": "sponsored_ad_1",
                        "creation_time": 1720000100,
                        "sponsored_data": {"ad_id": "ad_12345"},
                        "message": {"text": "광고 문구"},
                    }
                },
            ]
        }
    }

    posts, _ = extract_fb_posts_from_payloads([payload])
    assert [p.post_id for p in posts] == ["my_share_1"]
    assert posts[0].shared_post is not None
    assert posts[0].shared_post.author_name == "뉴스페이지"
    assert posts[0].shared_post.text == "원본 뉴스 글"


def test_fb_friends_filter_non_user_pageitems_and_survive_broken_avatar(tmp_path: Path) -> None:
    payload = {
        "data": {
            "people_you_may_know": {
                "pageItems": {
                    "edges": [
                        {"node": {"id": "pymk_1", "name": "알수도있는사람", "url": "https://www.facebook.com/pymk1"}}
                    ]
                }
            },
            "pageItems": {
                "edges": [
                    {
                        "node": {
                            "id": "real_f1",
                            "title": {"text": "진짜친구1"},
                            "url": "https://www.facebook.com/realf1",
                            "image": {"uri": "https://cdn.fb.com/f1.jpg"},
                            "node": {"__typename": "User", "id": "real_f1"},
                        }
                    },
                    {
                        "node": {
                            "id": "real_f2",
                            "title": {"text": "진짜친구2"},
                            "url": "https://www.facebook.com/realf2",
                            "image": {"uri": "https://cdn.fb.com/missing_404.jpg"},
                            "node": {"__typename": "User", "id": "real_f2"},
                        }
                    },
                    {
                        "node": {
                            "id": "liked_page_1",
                            "title": {"text": "좋아요한 페이지"},
                            "url": "https://www.facebook.com/pages/somepage",
                            "node": {"__typename": "Page", "id": "liked_page_1"},
                        }
                    },
                    {
                        "node": {
                            "id": "group_1",
                            "title": {"text": "가입한 그룹"},
                            "url": "https://www.facebook.com/groups/devgroup",
                            "node": {"__typename": "Group", "id": "group_1"},
                        }
                    },
                ]
            },
        }
    }

    friends, _ = extract_fb_friends_from_payloads([payload])
    assert [f.full_name for f in friends] == ["진짜친구1", "진짜친구2"]

    routes = {
        "https://www.facebook.com/me/friends": (
            "<html></html>",
            [_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(payload))],
        )
    }
    out_dir = tmp_path / "fb_friends_resilient"
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)
    summary = FbCollector(
        _FakeFbPage(routes),
        FbCollectConfig(output_dir=out_dir, collect_pass="friends", cooldown=cooldown, max_scrolls=2),
        request_context=_FakeRequestContext({"https://cdn.fb.com/f1.jpg": _jpeg_bytes()}),
    ).run_pass_friends()

    assert summary.items_saved == 2
    saved = json.loads((out_dir / "friends.json").read_text(encoding="utf-8"))
    assert [c["full_name"] for c in saved] == ["진짜친구1", "진짜친구2"]
    assert saved[0]["avatar_local_path"] == "media/facebook/avatars/real_f1.jpg"
    assert saved[1]["avatar_local_path"] is None


def test_fb_incremental_batches_separate_files_profile_owner_priority_and_switch_cap(
    tmp_path: Path,
) -> None:
    from meta_archive_pipeline.collect.fb_collector import extract_fb_profile_from_payloads

    # 1. Profile extraction must prefer profile_owner even if a friend's Story appears first
    profile_payloads = [
        {
            "data": {
                "node": {
                    "__typename": "Story",
                    "post_id": "wall_post_1",
                    "actors": [{"id": "999", "name": "내담벼락에글쓴친구", "url": "https://www.facebook.com/friend_wall"}],
                }
            }
        },
        {
            "data": {
                "profile_owner": {
                    "id": "100001",
                    "name": "민윤기",
                    "url": "https://www.facebook.com/dbstnsdl12",
                    "profilePicLarge": {"uri": "https://cdn.fb.com/me.jpg"},
                }
            }
        },
    ]
    prof, av = extract_fb_profile_from_payloads(profile_payloads)
    assert prof is not None and prof.full_name == "민윤기" and prof.username == "dbstnsdl12"
    assert av == "https://cdn.fb.com/me.jpg"

    # 2. switch_to_all_comments must cap iterations even if button stays visible forever
    class _StuckSortPage(_FakeFbPage):
        def get_by_role(self, role: str, name: str) -> _FakeLocator:
            loc = super().get_by_role(role, name)
            if name in ("가장 관련성 높은 댓글", "모든 댓글"):
                self.visible_counts[name] = 999
            return loc

    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)
    stuck_page = _StuckSortPage({})
    switched = switch_to_all_comments(stuck_page, cooldown, max_switches=3)
    assert switched == 3

    # 3. Incremental scroll must write separate batch_0001.json and batch_0002.json (O(N), not O(N^2))
    #    and preserve original caption/media when a later scroll delivers only comment delta
    story_batch1 = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "p_split_1",
                "creation_time": 1730000000,
                "message": {"text": "원본 캡션 보존 필수"},
                "attachments": [{"media": {"image": {"uri": "https://cdn.fb.com/split1.jpg"}}}],
            }
        }
    }
    story_batch2_delta = {
        "data": {
            "node": {
                "__typename": "Story",
                "post_id": "p_split_1",
                "feedback": {
                    "display_comments": {
                        "edges": [
                            {
                                "node": {
                                    "id": "c_later_1",
                                    "created_time": 1730000100,
                                    "author": {"name": "친구B"},
                                    "body": {"text": "두번째 배치 댓글"},
                                }
                            }
                        ]
                    }
                },
            }
        }
    }

    class _TwoBatchPage(_FakeFbPage):
        def __init__(self) -> None:
            super().__init__(
                {
                    "https://www.facebook.com/me": (
                        "<html></html>",
                        [_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(story_batch1))],
                    )
                }
            )
            self.scrolls = 0

        def evaluate(self, script: str):
            if "scrollTo" in script:
                self.scrolls += 1
                if self.scrolls == 1:
                    for fn in self._handlers.get("response", []):
                        fn(_FakeResp("https://www.facebook.com/api/graphql/", 200, json.dumps(story_batch2_delta)))
                return None
            return 1000 + min(self.scrolls, 1) * 500

    out_split = tmp_path / "fb_split"
    summary = FbCollector(
        _TwoBatchPage(),
        FbCollectConfig(output_dir=out_split, collect_pass="posts", cooldown=cooldown, max_scrolls=5, max_stagnant_scrolls=2),
        request_context=_FakeRequestContext({"https://cdn.fb.com/split1.jpg": _jpeg_bytes()}),
    ).run_pass_posts()

    assert summary.items_saved == 1
    assert (out_split / "raw" / "posts" / "batch_0001.json").exists()
    assert (out_split / "raw" / "posts" / "batch_0002.json").exists()
    # batch_0001 must NOT be overwritten with batch_0002's payload
    b1 = json.loads((out_split / "raw" / "posts" / "batch_0001.json").read_text(encoding="utf-8"))
    b2 = json.loads((out_split / "raw" / "posts" / "batch_0002.json").read_text(encoding="utf-8"))
    assert len(b1) == 1 and len(b2) == 1
    saved_post = json.loads((out_split / "posts" / "p_split_1.json").read_text(encoding="utf-8"))
    assert saved_post["caption"] == "원본 캡션 보존 필수"
    assert len(saved_post["media"]) == 1
    assert [c["text"] for c in saved_post["comments"]] == ["두번째 배치 댓글"]


def test_2026_comet_no_uzpf_ghost_duplicates_ufi_feedback_nested_attached_story_and_profile_header() -> None:
    from meta_archive_pipeline.collect.fb_collector import extract_fb_profile_from_payloads

    comet_2026_payloads = [
        {
            "data": {
                "primaryActions": [
                    {
                        "client_handler": {
                            "profile_action": {
                                "profile_owner": {"id": "100003736202459", "name": "민윤기"}
                            }
                        }
                    }
                ],
                "user": {
                    "profile_header_renderer": {
                        "user": {
                            "id": "100003736202459",
                            "name": "민윤기",
                            "url": "https://www.facebook.com/dbstnsdl12",
                            "profilePicLarge": {"uri": "https://cdn.fb.com/me200.jpg"},
                        }
                    },
                    "timeline_list_feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "__typename": "Story",
                                    "id": "UzpfSTEwMDAwMzczNjIwMjQ1OTozMzYxNTcxMjU3MzEwNzE1",
                                    "post_id": "3361571257310715",
                                    "creation_time": 1738604126,
                                    "actors": [{"id": "100003736202459", "name": "민윤기", "url": "https://www.facebook.com/dbstnsdl12"}],
                                    "attached_story": {
                                        "id": "Uzpf_shallow",
                                        "permalink_url": "https://www.facebook.com/groups/550561588856217/posts/1792598711319159/",
                                    },
                                    "comet_sections": {
                                        "context_layout": {
                                            "story": {
                                                "id": "UzpfSTEwMDAwMzczNjIwMjQ1OTozMzYxNTcxMjU3MzEwNzE1",
                                                "actors": [{"id": "100003736202459", "name": "민윤기"}],
                                                "comet_sections": {"actor_photo": {"story": {"id": "Uzpf_inner"}}},
                                            }
                                        },
                                        "content": {
                                            "story": {
                                                "id": "UzpfSTEwMDAwMzczNjIwMjQ1OTozMzYxNTcxMjU3MzEwNzE1",
                                                "post_id": "3361571257310715",
                                                "attached_story": {
                                                    "actors": [{"name": "임진호"}],
                                                    "comet_sections": {
                                                        "attached_story": {
                                                            "story": {
                                                                "attached_story": {
                                                                    "comet_sections": {
                                                                        "attached_story_layout": {
                                                                            "story": {
                                                                                "message": {
                                                                                    "text": "계속 프로그래밍을 해도 될까?"
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    },
                                                },
                                            }
                                        },
                                        "feedback": {
                                            "story": {
                                                "id": "UzpfSTEwMDAwMzczNjIwMjQ1OTozMzYxNTcxMjU3MzEwNzE1",
                                                "story_ufi_container": {
                                                    "story": {
                                                        "post_id": "3361571257310715",
                                                        "feedback_context": {
                                                            "feedback_target_with_context": {
                                                                "comet_ufi_summary_and_actions_renderer": {
                                                                    "feedback": {
                                                                        "top_reactions": {
                                                                            "edges": [
                                                                                {
                                                                                    "reaction_count": 7,
                                                                                    "node": {"reaction_type": "LIKE"},
                                                                                }
                                                                            ]
                                                                        },
                                                                        "adaptive_ufi_action_renderers": [
                                                                            {
                                                                                "feedback": {
                                                                                    "reaction_count": {"count": 7}
                                                                                }
                                                                            }
                                                                        ],
                                                                    }
                                                                },
                                                                "comment_rendering_instance": {
                                                                    "comments": {
                                                                        "edges": [
                                                                            {
                                                                                "node": {
                                                                                    "id": "c_2026_1",
                                                                                    "created_time": 1738605000,
                                                                                    "author": {"name": "친구C"},
                                                                                    "body": {"text": "좋은 글이네요"},
                                                                                }
                                                                            }
                                                                        ]
                                                                    }
                                                                },
                                                            }
                                                        },
                                                    }
                                                },
                                            }
                                        },
                                    },
                                }
                            }
                        ]
                    },
                },
            }
        }
    ]

    posts, _ = extract_fb_posts_from_payloads(comet_2026_payloads)
    assert [p.post_id for p in posts] == ["3361571257310715"]
    p = posts[0]
    assert p.reactions.total == 7
    assert p.reactions.by_type == {"LIKE": 7}
    assert [c.text for c in p.comments] == ["좋은 글이네요"]
    assert p.shared_post is not None
    assert p.shared_post.url == "https://www.facebook.com/groups/550561588856217/posts/1792598711319159/"
    assert p.shared_post.author_name == "임진호"
    assert p.shared_post.text == "계속 프로그래밍을 해도 될까?"

    prof, av = extract_fb_profile_from_payloads(comet_2026_payloads)
    assert prof is not None
    assert prof.user_id == "100003736202459"
    assert prof.full_name == "민윤기"
    assert prof.username == "dbstnsdl12"
    assert av == "https://cdn.fb.com/me200.jpg"


