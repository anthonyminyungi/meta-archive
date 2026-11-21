"""Tests for Instagram DASH/SSR/GraphQL/v1 parsers and CDP collector orchestration."""

from __future__ import annotations

import io
import json
import random
import struct
from pathlib import Path

from PIL import Image

from meta_archive_pipeline.collect.cdp import CooldownTracker
from meta_archive_pipeline.collect.ig_collector import (
    IgCollectConfig,
    IgCollector,
    extract_ig_connections_from_payloads,
    extract_ig_highlights_from_payloads,
    extract_ig_likers_from_payloads,
    extract_ig_post_from_payloads,
    extract_ig_profile_from_payloads,
    extract_ig_shortcodes_from_payloads,
    parse_dash_manifest,
)
from meta_archive_pipeline.utils.checkpoint import CheckpointStore


def _jpeg_bytes(color: tuple[int, int, int] = (10, 100, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _synthetic_mp4(*, include_audio: bool) -> bytes:
    def box(t: bytes, payload: bytes) -> bytes:
        return struct.pack(">I4s", 8 + len(payload), t) + payload

    def hdlr(h: bytes) -> bytes:
        return box(b"hdlr", b"\x00\x00\x00\x00\x00\x00\x00\x00" + h)

    traks = [box(b"trak", box(b"mdia", hdlr(b"vide")))]
    if include_audio:
        traks.append(box(b"trak", box(b"mdia", hdlr(b"soun"))))
    return (
        box(b"ftyp", b"isom\x00\x00\x02\x00isom")
        + box(b"moov", b"".join(traks))
        + box(b"mdat", b"\x00" * 32)
    )


def test_parse_dash_manifest_selects_highest_bandwidth() -> None:
    mpd = """<?xml version="1.0" encoding="UTF-8"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v1" bandwidth="500000">
            <BaseURL>https://cdn.example.com/v_low.mp4</BaseURL>
          </Representation>
          <Representation id="v2" bandwidth="1800000">
            <BaseURL>https://cdn.example.com/v_high.mp4</BaseURL>
          </Representation>
        </AdaptationSet>
        <AdaptationSet>
          <Representation id="a1" mimeType="audio/mp4" bandwidth="64000">
            <BaseURL>https://cdn.example.com/a_low.m4a</BaseURL>
          </Representation>
          <Representation id="a2" mimeType="audio/mp4" bandwidth="128000">
            <BaseURL>https://cdn.example.com/a_high.m4a</BaseURL>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    v_url, a_url = parse_dash_manifest(mpd)
    assert v_url == "https://cdn.example.com/v_high.mp4"
    assert a_url == "https://cdn.example.com/a_high.m4a"

    video_only_mpd = """
    <MPD>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation bandwidth="900000"><BaseURL>https://cdn.example.com/v.mp4</BaseURL></Representation>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    assert parse_dash_manifest(video_only_mpd) == ("https://cdn.example.com/v.mp4", None)
    assert parse_dash_manifest("not xml") == (None, None)


def test_extract_profile_shortcodes_and_post_carousel_dash_and_comments() -> None:
    profile_gql = {
        "data": {
            "user": {
                "id": "1001",
                "username": "yungis_",
                "full_name": "윤기",
                "biography": "기록 계정",
                "profile_pic_url_hd": "https://cdn.example.com/avatar.jpg",
                "edge_owner_to_timeline_media": {
                    "count": 2,
                    "edges": [
                        {"node": {"id": "p1", "shortcode": "SC_CAROUSEL"}},
                        {"node": {"id": "p2", "code": "SC_SINGLE"}},
                    ],
                },
                "edge_followed_by": {"count": 107},
                "edge_follow": {"count": 161},
            }
        }
    }
    profile_v1 = {
        "user": {
            "pk": 1001,
            "username": "yungis_",
            "media_count": 2,
            "follower_count": 107,
            "following_count": 161,
        }
    }

    profile, avatar_url = extract_ig_profile_from_payloads([profile_v1, profile_gql])
    assert profile is not None
    assert profile.username == "yungis_"
    assert profile.full_name == "윤기"
    assert profile.counts.posts == 2
    assert profile.counts.followers == 107
    assert profile.counts.following == 161
    assert avatar_url == "https://cdn.example.com/avatar.jpg"

    shortcodes = extract_ig_shortcodes_from_payloads([profile_gql, {"status": "ok", "code": "200"}])
    assert shortcodes == ["SC_CAROUSEL", "SC_SINGLE"]

    dash_xml = """
    <MPD>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation bandwidth="1200000"><BaseURL>https://cdn.example.com/dash_v.mp4</BaseURL></Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="audio/mp4">
          <Representation bandwidth="128000"><BaseURL>https://cdn.example.com/dash_a.m4a</BaseURL></Representation>
        </AdaptationSet>
      </Period>
    </MPD>
    """
    ssr_post_payload = {
        "data": {
            "xdt_shortcode_media": {
                "id": "99999_1001",
                "shortcode": "SC_CAROUSEL",
                "taken_at_timestamp": 1700000000,
                "owner": {"id": "1001", "username": "yungis_", "full_name": "윤기"},
                "edge_media_to_caption": {"edges": [{"node": {"text": "제주 여행 #제주 #바다 #제주"}}]},
                "location": {"id": "loc1", "name": "협재해수욕장", "lat": 33.394, "lng": 126.239},
                "edge_media_to_tagged_user": {
                    "edges": [{"node": {"user": {"id": "2002", "username": "friend_a", "full_name": "친구"}}}]
                },
                "edge_media_preview_like": {"count": 42},
                "coauthor_producers": [{"username": "co_author"}],
                "edge_sidecar_to_children": {
                    "edges": [
                        {
                            "node": {
                                "id": "m1",
                                "is_video": False,
                                "display_url": "https://cdn.example.com/slide1.jpg",
                                "dimensions": {"width": 1080, "height": 1350},
                            }
                        },
                        {
                            "node": {
                                "id": "m2",
                                "is_video": True,
                                "video_url": "https://cdn.example.com/slide2.mp4",
                                "video_dash_manifest": dash_xml,
                            }
                        },
                    ]
                },
                "edge_media_to_parent_comment": {
                    "edges": [
                        {
                            "node": {
                                "id": "c1",
                                "text": "사진 좋네요!",
                                "created_at": 1700000100,
                                "owner": {"username": "alice", "full_name": "앨리스"},
                                "edge_liked_by": {"count": 1},
                                "edge_threaded_comments": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "r1",
                                                "text": "고마워!",
                                                "created_at": 1700000200,
                                                "owner": {"username": "yungis_"},
                                            }
                                        }
                                    ]
                                },
                            }
                        }
                    ]
                },
            }
        }
    }
    xhr_comments_payload = {
        "comments": [
            {
                "pk": "c1",
                "media_id": "99999",
                "text": "사진 좋네요!",
                "created_at": 1700000100,
                "user": {"username": "alice", "full_name": "앨리스"},
                "comment_like_count": 3,
                "child_comments": [
                    {"pk": "r1", "text": "고마워!", "created_at": 1700000200, "user": {"username": "yungis_"}},
                    {"pk": "r2", "text": "나도 갈래", "created_at": 1700000300, "user": {"username": "bob"}},
                ],
            },
            {
                "pk": "c2",
                "media_id": "99999",
                "text": "두번째 댓글",
                "created_at": 1700000400,
                "user": {"username": "bob"},
            },
        ]
    }

    post, candidates = extract_ig_post_from_payloads("SC_CAROUSEL", [ssr_post_payload, xhr_comments_payload])
    assert post is not None
    assert post.post_type == "carousel"
    assert post.hashtags == ["제주", "바다"]
    assert post.location is not None and post.location.name == "협재해수욕장"
    assert [u.username for u in post.tagged_users] == ["friend_a"]
    assert post.likes.count == 42
    assert post.is_collab is True
    assert len(candidates) == 2
    assert candidates[0].is_carousel_cover is True
    assert candidates[0].progressive_url == "https://cdn.example.com/slide1.jpg"
    assert candidates[1].media_type == "video"
    assert candidates[1].dash_video_url == "https://cdn.example.com/dash_v.mp4"
    assert candidates[1].dash_audio_url == "https://cdn.example.com/dash_a.m4a"

    assert len(post.comments) == 2
    assert post.comments[0].comment_id == "c1"
    assert post.comments[0].like_count == 3
    assert [r.comment_id for r in post.comments[0].replies] == ["r1", "r2"]
    assert post.comments[1].comment_id == "c2"


def test_extract_likers_connections_and_highlights() -> None:
    likers = extract_ig_likers_from_payloads(
        [
            {"users": [{"pk": "1", "username": "u1", "full_name": "유저1"}]},
            {"edge_liked_by": {"edges": [{"node": {"id": "1", "username": "u1"}}, {"node": {"id": "2", "username": "u2"}}]}},
        ]
    )
    assert [u.username for u in likers] == ["u1", "u2"]

    conns, avatars = extract_ig_connections_from_payloads(
        [{"users": [{"pk": "11", "username": "f1", "full_name": "팔로워1", "profile_pic_url": "https://cdn/f1.jpg"}]}],
        "follower",
    )
    assert len(conns) == 1
    assert conns[0].relation == "follower"
    assert avatars == {"f1": "https://cdn/f1.jpg"}

    highlights_payload = {
        "data": {
            "highlights": {
                "edge_highlight_reels": {
                    "edges": [
                        {
                            "node": {
                                "id": "highlight:555",
                                "title": "여행",
                                "cover_media": {"cropped_image_version": {"url": "https://cdn/cov.jpg"}},
                            }
                        }
                    ]
                }
            },
            "reels_media": [
                {
                    "id": "highlight:555",
                    "items": [
                        {
                            "id": "st_1",
                            "taken_at_timestamp": 1710000000,
                            "display_url": "https://cdn/st1.jpg",
                        }
                    ],
                }
            ],
        }
    }
    stories, media_urls = extract_ig_highlights_from_payloads([highlights_payload])
    assert len(stories) == 1
    assert stories[0].highlight_title == "여행"
    assert stories[0].source == "highlight_web"
    assert media_urls["st_1"] == "https://cdn/st1.jpg"


class _FakeLocator:
    def __init__(self, clicks_ref: list[str], label: str, remaining: dict[str, int]) -> None:
        self._clicks = clicks_ref
        self._label = label
        self._remaining = remaining

    def count(self) -> int:
        return 1 if self._remaining.get(self._label, 0) > 0 else 0

    def nth(self, idx: int) -> _FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._remaining.get(self._label, 0) > 0

    def click(self) -> None:
        self._remaining[self._label] -= 1
        self._clicks.append(self._label)


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


class _FakeIgPage:
    def __init__(self, routes: dict[str, tuple[str, list[_FakeResp]]]) -> None:
        self._routes = routes
        self._handlers: dict[str, list] = {}
        self.url = ""
        self._html = ""
        self.clicked_buttons: list[str] = []
        self.remaining_buttons = {"댓글 더 보기": 1}

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
        return _FakeLocator(self.clicked_buttons, name, self.remaining_buttons)


def test_ig_collector_pass_core_smoke_and_429_pause(tmp_path: Path) -> None:
    blobs = {
        "https://cdn.example.com/avatar.jpg": _jpeg_bytes((20, 20, 20)),
        "https://cdn.example.com/sc1.jpg": _jpeg_bytes((100, 150, 200)),
        "https://cdn.example.com/sc2.mp4": _synthetic_mp4(include_audio=True),
    }

    profile_ssr = json.dumps(
        {
            "user": {
                "id": "10",
                "username": "yungis_",
                "profile_pic_url_hd": "https://cdn.example.com/avatar.jpg",
                "edge_owner_to_timeline_media": {
                    "count": 3,
                    "edges": [
                        {"node": {"shortcode": "SC1"}},
                        {"node": {"shortcode": "SC2"}},
                        {"node": {"shortcode": "SC3"}},
                    ],
                },
                "edge_followed_by": {"count": 107},
                "edge_follow": {"count": 161},
            }
        }
    )
    post1_ssr = json.dumps(
        {
            "data": {
                "xdt_shortcode_media": {
                    "id": "101",
                    "shortcode": "SC1",
                    "taken_at_timestamp": 1700000000,
                    "display_url": "https://cdn.example.com/sc1.jpg",
                    "edge_media_to_caption": {"edges": [{"node": {"text": "첫 글"}}]},
                }
            }
        }
    )
    post2_ssr = json.dumps(
        {
            "data": {
                "xdt_shortcode_media": {
                    "id": "102",
                    "shortcode": "SC2",
                    "taken_at_timestamp": 1700001000,
                    "is_video": True,
                    "video_url": "https://cdn.example.com/sc2.mp4",
                    "edge_media_to_caption": {"edges": [{"node": {"text": "영상 글"}}]},
                }
            }
        }
    )

    routes = {
        "https://www.instagram.com/yungis_/": (
            f'<html><script type="application/json" data-sjs>{profile_ssr}</script></html>',
            [],
        ),
        "https://www.instagram.com/p/SC1/": (
            f'<html><script type="application/json">{post1_ssr}</script></html>',
            [_FakeResp("https://www.instagram.com/api/v1/media/101/comments/", 200, '{"comments": []}')],
        ),
        "https://www.instagram.com/p/SC2/": (
            f'<html><script type="application/json">{post2_ssr}</script></html>',
            [],
        ),
        "https://www.instagram.com/p/SC3/": (
            "<html></html>",
            [_FakeResp("https://www.instagram.com/graphql/query", 429, "rate limited")],
        ),
    }

    out_dir = tmp_path / "ig_out"
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None, rng=random.Random(0))

    # Run limited smoke test (2 posts)
    page = _FakeIgPage(routes)
    req_ctx = _FakeRequestContext(blobs)
    summary = IgCollector(
        page,
        IgCollectConfig(username="yungis_", output_dir=out_dir, limit=2, cooldown=cooldown),
        request_context=req_ctx,
    ).run_pass_core()

    assert summary.processed == 2
    assert summary.succeeded == 2
    assert summary.paused_reason is None
    assert "댓글 더 보기" in page.clicked_buttons
    assert (out_dir / "profile.json").exists()
    assert (out_dir / "posts" / "SC1.json").exists()
    assert (out_dir / "posts" / "SC2.json").exists()
    assert (out_dir / "raw" / "posts" / "SC1.json").exists()
    assert (out_dir / "media" / "posts" / "SC1_01.jpg").exists()
    assert (out_dir / "media" / "posts" / "SC2_01.mp4").exists()

    # Resume remaining items (SC3 hits 429 -> safe pause without losing SC1/SC2)
    page_resume = _FakeIgPage(routes)
    summary_resume = IgCollector(
        page_resume,
        IgCollectConfig(username="yungis_", output_dir=out_dir, cooldown=cooldown),
        request_context=req_ctx,
    ).run_pass_core()

    assert summary_resume.skipped_done == 2
    assert summary_resume.processed == 1
    assert summary_resume.failed == 1
    assert summary_resume.paused_reason is not None and "rate_limited" in summary_resume.paused_reason

    store = CheckpointStore(out_dir / "state_core.json")
    assert store.summary() == {"pending": 0, "fetched": 0, "media_saved": 0, "done": 2, "failed": 1}


def test_ig_collector_pass_likes_truncated_flag(tmp_path: Path) -> None:
    out_dir = tmp_path / "ig_likes"
    (out_dir / "posts").mkdir(parents=True)
    (out_dir / "posts" / "SC1.json").write_text(
        json.dumps({"platform": "instagram", "post_id": "1", "likes": {"count": 150}}),
        encoding="utf-8",
    )
    (out_dir / "index_shortcodes.json").write_text(json.dumps(["SC1"]), encoding="utf-8")

    routes = {
        "https://www.instagram.com/p/SC1/liked_by/": (
            "<html></html>",
            [
                _FakeResp(
                    "https://www.instagram.com/api/v1/media/1/likers/",
                    200,
                    json.dumps({"users": [{"pk": "1", "username": "liker1"}]}),
                )
            ],
        )
    }
    cooldown = CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0, sleep_fn=lambda _: None)
    summary = IgCollector(
        _FakeIgPage(routes),
        IgCollectConfig(username="yungis_", output_dir=out_dir, collect_pass="likes", cooldown=cooldown),
        request_context=_FakeRequestContext({}),
    ).run_pass_likes()

    assert summary.succeeded == 1
    likes_saved = json.loads((out_dir / "likes" / "SC1.json").read_text(encoding="utf-8"))
    assert likes_saved["count"] == 150
    assert likes_saved["is_truncated"] is True
    assert [u["username"] for u in likes_saved["users"]] == ["liker1"]
