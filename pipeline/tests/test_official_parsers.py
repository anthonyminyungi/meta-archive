"""Tests for Instagram (2026 schema) and Facebook (+ E2EE) official export parsers."""

from __future__ import annotations

import json
from pathlib import Path

from meta_archive_pipeline.official.fb_parser import parse_fb_official_export
from meta_archive_pipeline.official.ig_parser import parse_ig_official_export


def _garble(text: str) -> str:
    return text.encode("utf-8").decode("latin-1")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_parse_ig_official_export_2026_schema(tmp_path: Path) -> None:
    root = tmp_path / "ig_export"

    # Create referenced media files (paths only need exist conceptually for parser; parser doesn't read bytes)
    _write_json(
        root / "personal_information" / "personal_information" / "personal_information.json",
        {
            "profile_user": [
                {
                    "media_map_data": {"Profile Photo": {"uri": "media/other/avatar.jpg"}},
                    "string_map_data": {
                        "Username": {"value": "yungis_"},
                        "Name": {"value": _garble("윤기")},
                        "Bio": {"value": _garble("사진 기록 📷")},
                    },
                }
            ]
        },
    )

    _write_json(
        root / "your_instagram_activity" / "media" / "posts_1.json",
        [
            {
                "title": _garble("제주 여행 #바다"),
                "creation_timestamp": 1700000000,
                "media": [
                    {
                        "uri": "media/posts/202311/1001.jpg",
                        "creation_timestamp": 1700000000,
                        "media_metadata": {
                            "photo_metadata": {
                                "exif_data": [
                                    {
                                        "date_time_original": "2023:11:14 15:20:00",
                                        "latitude": 33.4996,
                                        "longitude": 126.5312,
                                    }
                                ]
                            }
                        },
                    },
                    {
                        "uri": "media/posts/202311/1002.mp4",
                        "creation_timestamp": 1700000000,
                    },
                ],
            }
        ],
    )

    _write_json(
        root / "your_instagram_activity" / "media" / "archived_posts.json",
        {
            "ig_archived_post_media": [
                {
                    "media": [
                        {
                            "uri": "media/archived_posts/201801/2001.jpg",
                            "creation_timestamp": 1515000000,
                            "title": _garble("보관 글"),
                        }
                    ]
                }
            ]
        },
    )

    # 2026 label_values posts.json with extra carousel cover + Place + Hashtags + Is audio muted
    _write_json(
        root / "your_instagram_activity" / "media" / "posts.json",
        [
            {
                "timestamp": 1700000000,
                "media": [],
                "label_values": [
                    {"label": "Media", "media": [{"uri": "media/posts/17978433167548651.jpg"}]},
                    {"label": "Creation time", "timestamp_value": 1700000000},
                    {"label": "Original width", "value": "1080"},
                    {"label": "Original height", "value": "1350"},
                    {
                        "title": "Media",
                        "dict": [
                            {"label": "Media", "media": [{"uri": "media/posts/202311/1001.jpg"}]},
                            {"label": "Original width", "value": "1080"},
                            {"label": "Original height", "value": "1350"},
                            {"label": "Media", "media": [{"uri": "media/posts/202311/1002.mp4"}]},
                            {"label": "Is audio muted", "value": "True"},
                        ],
                    },
                    {
                        "title": "Place",
                        "dict": [
                            {"label": "Name", "value": _garble("협재해변")},
                            {"label": "Description", "value": _garble("제주 한림읍")},
                        ],
                    },
                    {
                        "title": "Hashtags",
                        "dict": [{"label": "Name", "value": _garble("제주")}],
                    },
                ],
            }
        ],
    )

    # Stories: 1 normal with exif_data, 1 empty URI
    _write_json(
        root / "your_instagram_activity" / "media" / "stories.json",
        {
            "ig_stories": [
                {
                    "uri": "media/stories/202401/3001.jpg",
                    "creation_timestamp": 1705000000,
                    "media_metadata": {
                        "photo_metadata": {"exif_data": [{"date_time_original": "2024:01:11 10:00:00"}]}
                    },
                },
                {
                    "uri": "",
                    "creation_timestamp": 1705000500,
                },
            ]
        },
    )

    # Connections: followers_1 uses value; following uses title only
    _write_json(
        root / "connections" / "followers_and_following" / "followers_1.json",
        [
            {
                "title": "",
                "string_list_data": [
                    {"href": "https://www.instagram.com/alice", "value": "alice", "timestamp": 1690000000}
                ],
            }
        ],
    )
    _write_json(
        root / "connections" / "followers_and_following" / "following.json",
        {
            "relationships_following": [
                {
                    "title": "bob_2026",
                    "string_list_data": [
                        {"href": "https://www.instagram.com/_u/bob_2026", "timestamp": 1695000000}
                    ],
                }
            ]
        },
    )

    # Multi-file DM thread
    _write_json(
        root / "your_instagram_activity" / "messages" / "inbox" / "friend_123" / "message_2.json",
        {
            "title": _garble("단톡"),
            "participants": [{"name": _garble("윤기")}, {"name": _garble("친구")}],
            "messages": [
                {"sender_name": _garble("친구"), "timestamp_ms": 1700000100000, "content": _garble("먼저 보낸 말")}
            ],
        },
    )
    _write_json(
        root / "your_instagram_activity" / "messages" / "inbox" / "friend_123" / "message_1.json",
        {
            "title": _garble("단톡"),
            "participants": [{"name": _garble("윤기")}, {"name": _garble("친구")}],
            "messages": [
                {
                    "sender_name": _garble("윤기"),
                    "timestamp_ms": 1700000200000,
                    "content": _garble("나중에 보낸 말 🎉"),
                    "reactions": [{"reaction": _garble("❤️"), "actor": _garble("친구")}],
                }
            ],
        },
    )

    bundle = parse_ig_official_export(root)
    assert bundle.platform == "instagram"
    assert bundle.profile is not None
    assert bundle.profile.username == "yungis_"
    assert bundle.profile.full_name == "윤기"
    assert bundle.profile.bio == "사진 기록 📷"
    assert bundle.profile.avatar_local_path == "media/other/avatar.jpg"

    assert len(bundle.posts) == 2
    active_post = next(p for p in bundle.posts if not p.is_archived)
    assert active_post.post_type == "carousel"
    assert active_post.location is not None and active_post.location.name == "협재해변"
    assert active_post.hashtags == ["바다", "제주"]
    assert len(active_post.media) == 3
    assert active_post.media[0].is_carousel_cover is True
    assert active_post.media[0].media_id == "17978433167548651"
    assert active_post.media[1].taken_at_exif == "2023:11:14 15:20:00"
    assert active_post.media[1].gps_lat == 33.4996
    assert active_post.media[2].is_audio_muted is True

    archived_post = next(p for p in bundle.posts if p.is_archived)
    assert archived_post.caption == "보관 글"

    assert len(bundle.stories) == 2
    assert bundle.stories[0].media is not None
    assert bundle.stories[0].media.taken_at_exif == "2024:01:11 10:00:00"
    assert bundle.stories[1].media is None
    assert any("empty URI" in w for w in bundle.warnings)

    assert [(c.relation, c.username) for c in bundle.connections] == [
        ("follower", "alice"),
        ("following", "bob_2026"),
    ]

    assert len(bundle.messages) == 1
    thread = bundle.messages[0]
    assert thread.title == "단톡"
    assert [m.text for m in thread.messages] == ["먼저 보낸 말", "나중에 보낸 말 🎉"]
    assert thread.messages[1].reactions[0].reaction == "❤️"


def test_parse_fb_official_and_e2ee_exports(tmp_path: Path) -> None:
    fb_root = tmp_path / "fb_export"
    e2ee_root = tmp_path / "fb_e2ee"

    _write_json(
        fb_root / "personal_information" / "profile_information" / "profile_information.json",
        {"profile_v2": {"name": {"full_name": _garble("윤기")}, "intro_bio": _garble("페북 소개")}},
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_posts_1.json",
        [
            {
                "timestamp": 1680000000,
                "data": [{"post": _garble("타임라인 글 #추억")}],
                "attachments": [
                    {"data": [{"media": {"uri": "your_facebook_activity/posts/media/a.jpg", "creation_timestamp": 1680000000}}]},
                    {"data": [{"place": {"name": _garble("서울역"), "coordinate": {"latitude": 37.55, "longitude": 126.97}}}]},
                ],
            }
        ],
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "album" / "0.json",
        {
            "name": _garble("모바일 업로드"),
            "photos": [
                {
                    "uri": "your_facebook_activity/posts/media/a.jpg",
                    "creation_timestamp": 1680000000,
                    "comments": [{"author": _garble("친구"), "comment": _garble("좋아요!"), "timestamp": 1680000100}],
                },
                {
                    "uri": "your_facebook_activity/posts/media/b.jpg",
                    "creation_timestamp": 1680005000,
                    "description": _garble("앨범 단독 사진"),
                },
            ],
        },
    )
    _write_json(
        fb_root / "connections" / "friends" / "your_friends.json",
        {"friends_v2": [{"name": _garble("홍길동"), "timestamp": 1600000000}]},
    )
    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "inbox" / "room1" / "message_1.json",
        {
            "title": _garble("일반 방"),
            "participants": [{"name": _garble("윤기")}],
            "messages": [{"sender_name": _garble("윤기"), "timestamp_ms": 1680000000000, "content": _garble("안녕")}],
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "e2ee_cutover" / "room2" / "message_1.json",
        {
            "title": _garble("암호화 전환 방"),
            "participants": [{"name": _garble("윤기")}],
            "messages": [{"sender_name": _garble("윤기"), "timestamp_ms": 1690000000000, "content": _garble("전환 전")}],
        },
    )
    _write_json(
        e2ee_root / "e2ee_thread.json",
        {
            "threadName": "종단간 대화방",
            "participants": ["윤기", "친구B"],
            "messages": [
                {
                    "senderName": "친구B",
                    "timestamp": 1710000000000,
                    "text": "정상 UTF-8 E2EE 메시지 🔒",
                    "isUnsent": False,
                }
            ],
        },
    )

    bundle = parse_fb_official_export(fb_root, e2ee_root=e2ee_root)
    assert bundle.platform == "facebook"
    assert bundle.profile is not None and bundle.profile.full_name == "윤기"
    assert len(bundle.posts) == 2
    assert bundle.posts[0].album_name == "모바일 업로드"
    assert bundle.posts[0].post_type == "album_photo"
    assert bundle.posts[0].comments[0].text == "좋아요!"
    assert bundle.posts[0].location is not None and bundle.posts[0].location.name == "서울역"
    assert bundle.posts[1].caption == "앨범 단독 사진"

    assert len(bundle.connections) == 1
    assert bundle.connections[0].full_name == "홍길동"
    assert bundle.connections[0].match_confidence == "heuristic"

    assert len(bundle.messages) == 3
    by_title = {t.title: t for t in bundle.messages}
    assert by_title["일반 방"].is_e2ee is False
    assert by_title["암호화 전환 방"].is_e2ee is True
    assert by_title["종단간 대화방"].is_e2ee is True
    assert by_title["종단간 대화방"].messages[0].text == "정상 UTF-8 E2EE 메시지 🔒"
