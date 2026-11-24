"""Tests for Facebook raw payload hydration and external OG link preview enricher."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
from PIL import Image

from meta_archive_pipeline.collect.fb_enricher import (
    _is_safe_public_url,
    enrich_facebook_collected,
    parse_og_metadata,
)
from meta_archive_pipeline.schema.models import Connection, Post, SharedPostRef


def _img_bytes(fmt: str = "JPEG", size: tuple[int, int] = (64, 64), color: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def test_parse_og_metadata_and_youtube_fallback() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="테스트 기사 제목" />
      <meta property="og:description" content="기사 요약입니다." />
      <meta property="og:image" content="/assets/og.png" />
    </head><body></body></html>
    """
    img, title, desc = parse_og_metadata(html, "https://example.com/posts/123")
    assert img == "https://example.com/assets/og.png"
    assert title == "테스트 기사 제목"
    assert desc == "기사 요약입니다."

    yt_img, _, _ = parse_og_metadata("<html></html>", "https://youtu.be/dQw4w9WgXcQ?t=10")
    assert yt_img == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


def test_is_safe_public_url_blocks_private_and_non_http() -> None:
    assert _is_safe_public_url("https://93.184.216.34/img.jpg", check_dns=False) is True
    assert _is_safe_public_url("http://127.0.0.1/secret.jpg", check_dns=False) is False
    assert _is_safe_public_url("http://169.254.169.254/latest/meta-data", check_dns=False) is False
    assert _is_safe_public_url("http://192.168.1.1/router.png", check_dns=False) is False
    assert _is_safe_public_url("http://localhost:8000/test", check_dns=False) is False
    assert _is_safe_public_url("file:///etc/passwd", check_dns=False) is False
    assert _is_safe_public_url("javascript:alert(1)", check_dns=False) is False


def test_enrich_facebook_collected_friends_raw_and_external_og(tmp_path: Path) -> None:
    col_dir = tmp_path / "facebook"
    (col_dir / "raw" / "posts").mkdir(parents=True)
    (col_dir / "posts").mkdir(parents=True)

    # Seed raw/friends.json + friends.json (without avatars yet)
    raw_friends = [
        {
            "data": {
                "node": {
                    "pageItems": {
                        "edges": [
                            {
                                "node": {
                                    "__typename": "TimelineAppCollectionItem",
                                    "id": "friend_1",
                                    "title": {"text": "홍길동"},
                                    "url": "https://www.facebook.com/hong",
                                    "image_v2": {"uri": "https://scontent.fbcdn.net/f1.png"},
                                }
                            },
                            {
                                "node": {
                                    "__typename": "TimelineAppCollectionItem",
                                    "id": "friend_2",
                                    "title": {"text": "깨진이미지친구"},
                                    "url": "https://www.facebook.com/broken",
                                    "image_v2": {"uri": "https://scontent.fbcdn.net/broken.jpg"},
                                }
                            },
                        ]
                    }
                }
            }
        }
    ]
    (col_dir / "raw" / "friends.json").write_text(json.dumps(raw_friends, ensure_ascii=False), encoding="utf-8")
    existing_friends = [
        Connection(platform="facebook", relation="friend", user_id="friend_1", full_name="홍길동"),
        Connection(platform="facebook", relation="friend", user_id="friend_2", full_name="깨진이미지친구"),
    ]
    (col_dir / "friends.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in existing_friends], ensure_ascii=False),
        encoding="utf-8",
    )

    # Seed raw/posts/batch_0001.json + 3 posts
    raw_posts = [
        {
            "data": {
                "node": {
                    "__typename": "Story",
                    "post_id": "p_cdn",
                    "attachments": [
                        {
                            "styles": {
                                "attachment": {
                                    "url": "https://blog.example.com/article-1",
                                    "title_with_entities": {"text": "원본 기사"},
                                    "media": {
                                        "large_share_image": {
                                            "uri": "https://external.fbcdn.net/emg1/article1.jpg"
                                        }
                                    },
                                }
                            }
                        }
                    ],
                }
            }
        },
        {
            "data": {
                "node": {
                    "__typename": "Story",
                    "post_id": "p_og_fallback",
                    "attachments": [
                        {
                            "styles": {
                                "attachment": {
                                    "url": "https://news.example.com/live-post",
                                    "media": {
                                        "large_share_image": {
                                            "uri": "https://external.fbcdn.net/emg1/expired.jpg"
                                        }
                                    },
                                }
                            }
                        }
                    ],
                }
            }
        },
    ]
    (col_dir / "raw" / "posts" / "batch_0001.json").write_text(
        json.dumps(raw_posts, ensure_ascii=False), encoding="utf-8"
    )

    for post in (
        Post(
            platform="facebook",
            post_id="p_cdn",
            post_type="shared",
            shared_post=SharedPostRef(url="https://blog.example.com/article-1"),
        ),
        Post(
            platform="facebook",
            post_id="p_og_fallback",
            post_type="shared",
            shared_post=SharedPostRef(url="https://news.example.com/live-post"),
        ),
        Post(
            platform="facebook",
            post_id="p_tiny_pixel",
            post_type="shared",
            shared_post=SharedPostRef(url="https://tracker.example.com/page"),
        ),
    ):
        (col_dir / "posts" / f"{post.post_id}.json").write_text(
            post.model_dump_json(indent=2), encoding="utf-8"
        )

    calls: list[str] = []
    png_avatar = _img_bytes("PNG", (80, 80))
    jpg_thumb = _img_bytes("JPEG", (200, 120))
    tiny_pixel = _img_bytes("PNG", (1, 1))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == "https://scontent.fbcdn.net/f1.png":
            return httpx.Response(200, content=png_avatar, headers={"content-type": "image/png"})
        if url == "https://scontent.fbcdn.net/broken.jpg":
            return httpx.Response(200, content=b"<html>expired</html>", headers={"content-type": "text/html"})
        if url == "https://external.fbcdn.net/emg1/article1.jpg":
            return httpx.Response(200, content=jpg_thumb, headers={"content-type": "image/jpeg"})
        if url == "https://external.fbcdn.net/emg1/expired.jpg":
            return httpx.Response(403, content=b"URL signature expired")
        if url == "https://news.example.com/live-post":
            html = (
                '<html><head><meta property="og:title" content="라이브 OG 제목">'
                '<meta property="og:description" content="라이브 OG 요약">'
                '<meta property="og:image" content="https://cdn.example.com/og_banner.jpg"></head></html>'
            )
            return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
        if url == "https://cdn.example.com/og_banner.jpg":
            return httpx.Response(200, content=jpg_thumb, headers={"content-type": "image/jpeg"})
        if url == "https://tracker.example.com/page":
            html = '<html><head><meta property="og:image" content="https://tracker.example.com/px.png"></head></html>'
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if url == "https://tracker.example.com/px.png":
            return httpx.Response(200, content=tiny_pixel, headers={"content-type": "image/png"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        s1 = enrich_facebook_collected(col_dir, client=client, concurrency=2, check_dns=False)

    assert s1.friends_total == 2
    assert s1.friends_avatars_downloaded == 1
    assert s1.friends_avatars_failed == 1
    assert s1.thumbs_from_raw_downloaded == 1
    assert s1.thumbs_from_og_downloaded == 1
    assert s1.thumbs_failed == 1

    saved_friends = json.loads((col_dir / "friends.json").read_text(encoding="utf-8"))
    assert saved_friends[0]["avatar_local_path"] == "media/facebook/avatars/friend_1.png"
    assert saved_friends[1]["avatar_local_path"] is None
    assert (col_dir / "media" / "avatars" / "friend_1.png").exists()

    p_cdn = json.loads((col_dir / "posts" / "p_cdn.json").read_text(encoding="utf-8"))
    assert p_cdn["shared_post"]["thumbnail_local_path"] == "media/facebook/links/p_cdn_thumb.jpg"
    assert p_cdn["shared_post"]["title"] == "원본 기사"

    p_og = json.loads((col_dir / "posts" / "p_og_fallback.json").read_text(encoding="utf-8"))
    assert p_og["shared_post"]["thumbnail_local_path"] == "media/facebook/links/p_og_fallback_thumb.jpg"
    assert p_og["shared_post"]["title"] == "라이브 OG 제목"
    assert p_og["shared_post"]["text"] == "라이브 OG 요약"

    p_tiny = json.loads((col_dir / "posts" / "p_tiny_pixel.json").read_text(encoding="utf-8"))
    assert p_tiny["shared_post"]["thumbnail_local_path"] is None

    # Second run must reuse already-downloaded valid images without re-downloading them
    calls.clear()
    with httpx.Client(transport=transport) as client:
        s2 = enrich_facebook_collected(col_dir, client=client, concurrency=2, check_dns=False)
    assert s2.friends_avatars_skipped_existing == 1
    assert s2.friends_avatars_downloaded == 0
    assert s2.thumbs_skipped_existing == 2
    assert s2.thumbs_from_raw_downloaded == 0
    assert s2.thumbs_from_og_downloaded == 0
    assert "https://scontent.fbcdn.net/f1.png" not in calls
    assert "https://external.fbcdn.net/emg1/article1.jpg" not in calls
