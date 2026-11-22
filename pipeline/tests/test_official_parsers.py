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
            },
            {
                "timestamp": 1685000000,
                "attachments": [
                    {
                        "data": [
                            {
                                "external_context": {
                                    "name": _garble("유용한 블로그 글"),
                                    "url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.blog.example.org%2Fpost%2F1&h=1",
                                }
                            }
                        ]
                    }
                ],
            },
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
    assert len(bundle.posts) == 3
    assert bundle.posts[0].album_name == "모바일 업로드"
    assert bundle.posts[0].post_type == "album_photo"
    assert bundle.posts[0].comments[0].text == "좋아요!"
    assert bundle.posts[0].location is not None and bundle.posts[0].location.name == "서울역"
    shared_p = bundle.posts[1]
    assert shared_p.post_type == "shared"
    assert shared_p.shared_post is not None
    assert shared_p.shared_post.url == "https://www.blog.example.org/post/1"
    assert shared_p.shared_post.title == "유용한 블로그 글"
    assert shared_p.shared_post.domain == "blog.example.org"
    assert bundle.posts[2].caption == "앨범 단독 사진"

    assert len(bundle.connections) == 1
    assert bundle.connections[0].full_name == "홍길동"
    assert bundle.connections[0].match_confidence == "heuristic"

    assert len(bundle.messages) == 3
    by_title = {t.title: t for t in bundle.messages}
    assert by_title["일반 방"].is_e2ee is False
    assert by_title["암호화 전환 방"].is_e2ee is True
    assert by_title["종단간 대화방"].is_e2ee is True
    assert by_title["종단간 대화방"].messages[0].text == "정상 UTF-8 E2EE 메시지 🔒"


def test_parse_fb_official_extended_sources_and_avif(tmp_path: Path) -> None:
    fb_root = tmp_path / "fb_ext"

    _write_json(
        fb_root / "personal_information" / "profile_information" / "profile_information.json",
        {"profile_v2": {"name": {"full_name": _garble("윤기")}, "username": "dbstnsdl12"}},
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_posts__check_ins__photos_and_videos_1.json",
        [
            {
                "timestamp": 1680000000,
                "attachments": [
                    {
                        "data": [
                            {
                                "media": {
                                    "uri": "your_facebook_activity/posts/media/videos/111.mp4",
                                    "creation_timestamp": 1680000000,
                                }
                            }
                        ]
                    }
                ],
            }
        ],
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "album" / "5.json",
        {
            "name": _garble("프로필 사진"),
            "cover_photo": {
                "uri": "your_facebook_activity/posts/media/profile/999.avif",
                "creation_timestamp": 1700000000,
            },
            "photos": [
                {
                    "uri": "your_facebook_activity/posts/media/profile/999.avif",
                    "creation_timestamp": 1700000000,
                }
            ],
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_uncategorized_photos.json",
        {
            "other_photos_v2": [
                {
                    "uri": "your_facebook_activity/posts/media/your_posts/222.avif",
                    "creation_timestamp": 1600000000,
                    "description": _garble("미분류 사진 설명"),
                }
            ]
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_videos.json",
        {
            "videos_v2": [
                {
                    "uri": "your_facebook_activity/posts/media/videos/111.mp4",
                    "creation_timestamp": 1680000000,
                    "description": _garble("기존 영상 캡션 보강"),
                },
                {
                    "uri": "your_facebook_activity/posts/media/videos/333.mp4",
                    "creation_timestamp": 1681000000,
                    "description": _garble("단독 영상"),
                },
            ]
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "stories" / "archived_stories.json",
        {
            "archived_stories_v2": [
                {
                    "timestamp": 1574009400,
                    "attachments": [
                        {
                            "data": [
                                {
                                    "media": {
                                        "uri": "your_facebook_activity/posts/media/your_posts/444.jpg",
                                        "creation_timestamp": 1574096017,
                                    }
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "filtered_threads" / "flt1" / "message_1.json",
        {
            "title": _garble("필터링된 방"),
            "participants": [{"name": _garble("윤기")}],
            "messages": [
                {
                    "sender_name": _garble("윤기"),
                    "timestamp_ms": 1690000000000,
                    "gifs": [{"uri": "your_facebook_activity/messages/filtered_threads/flt1/gifs/1.gif"}],
                    "files": [{"uri": "your_facebook_activity/messages/filtered_threads/flt1/files/2.pdf"}],
                    "sticker": {"uri": "your_facebook_activity/messages/stickers_used/3.png"},
                }
            ],
        },
    )

    bundle = parse_fb_official_export(fb_root)
    assert bundle.profile is not None
    assert bundle.profile.avatar_local_path == "your_facebook_activity/posts/media/profile/999.avif"

    # 1 existing video post (caption enriched) + 1 album avif + 1 uncategorized avif + 1 extra video
    assert len(bundle.posts) == 4
    by_uri = {p.media[0].local_path: p for p in bundle.posts if p.media}
    assert by_uri["your_facebook_activity/posts/media/videos/111.mp4"].caption == "기존 영상 캡션 보강"
    assert by_uri["your_facebook_activity/posts/media/videos/333.mp4"].caption == "단독 영상"
    assert by_uri["your_facebook_activity/posts/media/profile/999.avif"].media[0].media_type == "image"
    uncat = by_uri["your_facebook_activity/posts/media/your_posts/222.avif"]
    assert uncat.media[0].media_type == "image"
    assert uncat.album_name == "미분류 사진"
    assert uncat.caption == "미분류 사진 설명"

    assert len(bundle.stories) == 1
    assert bundle.stories[0].platform == "facebook"
    assert bundle.stories[0].media is not None
    assert bundle.stories[0].media.local_path == "your_facebook_activity/posts/media/your_posts/444.jpg"

    assert len(bundle.messages) == 1
    flt = bundle.messages[0]
    assert flt.is_pending is True
    assert [(m.local_path, m.media_type) for m in flt.messages[0].media] == [
        ("your_facebook_activity/messages/filtered_threads/flt1/gifs/1.gif", "image"),
        ("your_facebook_activity/messages/filtered_threads/flt1/files/2.pdf", "unknown"),
        ("your_facebook_activity/messages/stickers_used/3.png", "image"),
    ]


def test_parse_ig_real_2026_wrapped_label_values_mp4_audio_and_activities(tmp_path: Path) -> None:
    root = tmp_path / "ig_real_2026"

    _write_json(
        root / "your_instagram_activity" / "media" / "posts_1.json",
        [
            {
                "title": _garble("캐러셀 본문"),
                "creation_timestamp": 1700000000,
                "media": [
                    {"uri": "media/posts/202311/s1.jpg", "creation_timestamp": 1700000000},
                    {"uri": "media/posts/202311/s2.mp4", "creation_timestamp": 1700000000},
                ],
            }
        ],
    )

    # Actual 2026 Instagram posts.json structure with anonymous `{"title": "", "dict": [...]}` wrappers
    _write_json(
        root / "your_instagram_activity" / "media" / "posts.json",
        [
            {
                "timestamp": 1700000000,
                "media": [],
                "label_values": [
                    {"label": "Media", "media": []},
                    {"label": "Creation time", "timestamp_value": 1700000000},
                    {"label": "Latitude", "value": "37.5665"},
                    {"label": "Longitude", "value": "126.9780"},
                    {
                        "title": "Media",
                        "dict": [
                            {
                                "title": "",
                                "dict": [
                                    {"label": "Media", "media": [{"uri": "media/posts/202311/s1.jpg"}]},
                                    {"label": "Original width", "value": "1440"},
                                    {"label": "Original height", "value": "1800"},
                                ],
                            },
                            {
                                "title": "",
                                "dict": [
                                    {"label": "Media", "media": [{"uri": "media/posts/202311/s2.mp4"}]},
                                    {"label": "Is audio muted", "value": "True"},
                                    {
                                        "label": "Metadata",
                                        "dict": [
                                            {"label": "duration", "value": "8.5"},
                                            {"label": "width", "value": "1080"},
                                            {"label": "height", "value": "1920"},
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "title": "Place",
                        "dict": [
                            {
                                "title": "",
                                "dict": [
                                    {"label": "Name", "value": _garble("광화문광장")},
                                    {"label": "Description", "value": _garble("서울 종로구")},
                                ],
                            }
                        ],
                    },
                    {
                        "title": "Hashtags",
                        "dict": [
                            {"title": "", "dict": [{"label": "Name", "value": _garble("#서울")}]},
                        ],
                    },
                ],
            }
        ],
    )

    _write_json(
        root / "your_instagram_activity" / "media" / "stories.json",
        {
            "ig_stories": [
                {
                    "uri": "media/stories/202405/st1.jpg",
                    "creation_timestamp": 1715000000,
                    "title": _garble("스토리 캡션 텍스트"),
                }
            ]
        },
    )

    # Group DM with custom thread image + .mp4 voice message + rich share metadata
    _write_json(
        root / "your_instagram_activity" / "messages" / "inbox" / "grp_1" / "message_1.json",
        {
            "title": _garble("우리 단톡"),
            "image": {"uri": "your_instagram_activity/messages/photos/grp_cover.jpg"},
            "participants": [{"name": _garble("윤기")}],
            "messages": [
                {
                    "sender_name": _garble("윤기"),
                    "timestamp_ms": 1715000100000,
                    "audio_files": [{"uri": "your_instagram_activity/messages/inbox/grp_1/audio/voice1.mp4"}],
                    "share": {
                        "link": "https://www.instagram.com/reel/REEL99/",
                        "share_text": _garble("재밌는 릴스 캡션"),
                        "original_content_owner": "funny_creator",
                    },
                }
            ],
        },
    )

    # Own comments, saved posts, and reposts
    _write_json(
        root / "your_instagram_activity" / "comments" / "post_comments_1.json",
        [
            {
                "media_list_data": [],
                "string_map_data": {
                    "Comment": {"value": _garble("내가 쓴 인스타 댓글")},
                    "Media Owner": {"value": "friend_owner"},
                    "Time": {"timestamp": 1710000000},
                },
            }
        ],
    )
    _write_json(
        root / "your_instagram_activity" / "comments" / "reels_comments.json",
        {
            "comments_reels_comments": [
                {
                    "string_map_data": {
                        "Comment": {"value": _garble("내가 쓴 릴스 댓글")},
                        "Media Owner": {"value": "reel_owner"},
                        "Time": {"timestamp": 1710000500},
                    }
                }
            ]
        },
    )
    _write_json(
        root / "your_instagram_activity" / "saved" / "saved_posts.json",
        [
            {
                "timestamp": 1712000000,
                "media": [],
                "label_values": [
                    {"label": "URL", "href": "https://www.instagram.com/p/SAVED1/", "value": "https://www.instagram.com/p/SAVED1/"},
                    {"label": "Caption", "value": _garble("저장해둔 맛집 리스트")},
                    {
                        "title": "Owner",
                        "dict": [
                            {
                                "title": "",
                                "dict": [
                                    {"label": "Username", "value": "foodie_kr"},
                                    {"label": "Name", "value": _garble("맛집탐방")},
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    )
    _write_json(
        root / "your_instagram_activity" / "media" / "reposts.json",
        {
            "timestamp": 1713000000,
            "media": [],
            "label_values": [
                {"label": "Text", "value": _garble("리포스트 메모")},
                {
                    "title": "Media",
                    "dict": [
                        {
                            "title": "",
                            "dict": [
                                {"label": "URL", "href": "https://www.instagram.com/p/REP1/", "value": "https://www.instagram.com/p/REP1/"},
                                {"label": "Caption", "value": _garble("원본 리포스트 캡션")},
                            ],
                        }
                    ],
                },
            ],
        },
    )

    bundle = parse_ig_official_export(root)
    # Must NOT duplicate the carousel into a second ig_postsjson_* post!
    assert len(bundle.posts) == 1
    p = bundle.posts[0]
    assert p.post_type == "carousel"
    assert p.location is not None
    assert p.location.name == "광화문광장"
    assert p.location.description == "서울 종로구"
    assert p.location.lat == 37.5665
    assert p.hashtags == ["서울"]
    assert p.media[0].width == 1440
    assert p.media[1].is_audio_muted is True
    assert p.media[1].duration_sec == 8.5
    assert p.media[1].width == 1080

    assert bundle.stories[0].caption == "스토리 캡션 텍스트"

    thread = bundle.messages[0]
    assert thread.avatar_local_path == "your_instagram_activity/messages/photos/grp_cover.jpg"
    msg = thread.messages[0]
    assert msg.media[0].media_type == "audio"
    assert msg.shared_link == "https://www.instagram.com/reel/REEL99/"
    assert msg.share_text == "재밌는 릴스 캡션"
    assert msg.share_owner == "funny_creator"

    by_kind = {a.kind: [x for x in bundle.activities if x.kind == a.kind] for a in bundle.activities}
    assert len(by_kind["own_comment"]) == 2
    assert by_kind["saved_item"][0].url == "https://www.instagram.com/p/SAVED1/"
    assert by_kind["saved_item"][0].target_author == "foodie_kr"
    assert by_kind["repost"][0].url == "https://www.instagram.com/p/REP1/"


def test_parse_fb_real_2026_taken_timestamp_groups_following_and_activities(tmp_path: Path) -> None:
    fb_root = tmp_path / "fb_real_2026"

    # Timeline post: system `title` without `data[].post` must go to `activity_title`, NOT `caption`!
    # Also test `tags` and `life_event` and `taken_timestamp` EXIF.
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_posts__check_ins__photos_and_videos_1.json",
        [
            {
                "timestamp": 1680000000,
                "title": _garble("윤기님이 링크를 공유했습니다."),
                "data": [{"update_timestamp": 1680000005}],
                "tags": [{"name": _garble("김철수")}],
                "attachments": [
                    {
                        "data": [
                            {
                                "external_context": {
                                    "url": "https://example.org/article",
                                }
                            }
                        ]
                    }
                ],
            },
            {
                "timestamp": 1690000000,
                "title": _garble("윤기님이 중요 이벤트를 추가했습니다."),
                "attachments": [
                    {"data": [{"life_event": {"title": _garble("대학 졸업")}}]},
                ],
            },
        ],
    )

    # Empty album (photos=[]) with cover_photo must still preserve cover_photo!
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "album" / "3.json",
        {
            "name": _garble("삼성전자 S 아카데미 Photos"),
            "photos": [],
            "cover_photo": {
                "uri": "your_facebook_activity/posts/media/your_posts/3450136083.png",
                "creation_timestamp": 1400000000,
            },
            "last_modified_timestamp": 1400000000,
        },
    )

    # Uncategorized photo with integer `taken_timestamp` in exif_data
    _write_json(
        fb_root / "your_facebook_activity" / "posts" / "your_uncategorized_photos.json",
        {
            "other_photos_v2": [
                {
                    "uri": "your_facebook_activity/posts/media/your_posts/cam1.avif",
                    "creation_timestamp": 1600000500,
                    "media_metadata": {
                        "photo_metadata": {"exif_data": [{"taken_timestamp": 1599990000}]}
                    },
                }
            ]
        },
    )

    # Group posts with PDF / extensionless file in `files/`
    _write_json(
        fb_root / "your_facebook_activity" / "groups" / "group_posts_and_comments.json",
        {
            "group_posts_v2": [
                {
                    "timestamp": 1580000000,
                    "title": _garble("윤기님이 컴퓨터공학과 학회 그룹에 게시물을 남겼습니다."),
                    "data": [{"post": _garble("발표 자료 공유합니다.")}],
                    "attachments": [
                        {"data": [{"media": {"uri": "files/204712957226567"}}]},
                        {"data": [{"media": {"uri": "files/1014542048960874.pdf"}}]},
                    ],
                }
            ]
        },
    )

    # Following list (`who_you've_followed.json`)
    _write_json(
        fb_root / "connections" / "followers" / "who_you've_followed.json",
        {"following_v3": [{"name": _garble("박지성"), "timestamp": 1500000000}]},
    )

    # Messenger: .mp4 audio_files, extensionless photo, missed call, share_text
    _write_json(
        fb_root / "your_facebook_activity" / "messages" / "inbox" / "friend_a" / "message_1.json",
        {
            "title": _garble("친구A"),
            "participants": [{"name": _garble("친구A")}],
            "messages": [
                {
                    "sender_name": _garble("친구A"),
                    "timestamp_ms": 1700000000000,
                    "audio_files": [{"uri": "your_facebook_activity/messages/inbox/friend_a/audio/clip.mp4"}],
                    "photos": [{"uri": "your_facebook_activity/messages/inbox/friend_a/photos/noext_img"}],
                    "share": {"link": "https://fb.com/watch/1", "share_text": _garble("공유 설명")},
                    "missed": True,
                    "call_duration": 0,
                }
            ],
        },
    )

    # FB user activities: own comments (+ sticker), group comments, saved items
    _write_json(
        fb_root / "your_facebook_activity" / "comments_and_reactions" / "comments.json",
        {
            "comments_v2": [
                {
                    "timestamp": 1650000000,
                    "title": _garble("윤기님이 이영희님의 게시물에 댓글을 남겼습니다."),
                    "data": [{"comment": {"timestamp": 1650000000, "comment": _garble("축하해요!"), "author": _garble("윤기")}}],
                    "attachments": [
                        {"data": [{"media": {"uri": "your_facebook_activity/posts/media/stickers_used/st1.png"}}]}
                    ],
                }
            ]
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "groups" / "your_comments_in_groups.json",
        {
            "group_comments_v2": [
                {
                    "timestamp": 1651000000,
                    "title": _garble("윤기님이 스터디모임 그룹의 게시물에 댓글을 남겼습니다."),
                    "data": [
                        {
                            "comment": {
                                "timestamp": 1651000000,
                                "comment": _garble("참석합니다!"),
                                "author": _garble("윤기"),
                                "group": _garble("스터디모임"),
                            }
                        }
                    ],
                }
            ]
        },
    )
    _write_json(
        fb_root / "your_facebook_activity" / "saved_items_and_collections" / "your_saved_items.json",
        {
            "saves_v2": [
                {
                    "timestamp": 1660000000,
                    "title": _garble("윤기님이 링크를 저장했습니다."),
                    "attachments": [
                        {
                            "data": [
                                {
                                    "external_context": {
                                        "name": _garble("좋은 개발 아티클"),
                                        "url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fdev.example.io%2F1",
                                    }
                                }
                            ]
                        }
                    ],
                }
            ]
        },
    )

    bundle = parse_fb_official_export(fb_root)

    shared_p = next(p for p in bundle.posts if p.post_type == "shared")
    assert shared_p.caption == ""
    assert shared_p.activity_title == "윤기님이 링크를 공유했습니다."
    assert [u.full_name for u in shared_p.tagged_users] == ["김철수"]

    life_p = next(p for p in bundle.posts if p.timestamp_unix == 1690000000)
    assert life_p.caption == "대학 졸업"
    assert life_p.activity_title == "윤기님이 중요 이벤트를 추가했습니다."

    cover_only = next(p for p in bundle.posts if p.album_name == "삼성전자 S 아카데미 Photos")
    assert cover_only.media[0].local_path == "your_facebook_activity/posts/media/your_posts/3450136083.png"

    uncat = next(p for p in bundle.posts if p.album_name == "미분류 사진")
    assert uncat.media[0].taken_at_unix == 1599990000

    grp_p = next(p for p in bundle.posts if p.post_type == "group_post")
    assert grp_p.group_name == "컴퓨터공학과 학회"
    assert grp_p.caption == "발표 자료 공유합니다."
    assert len(grp_p.media) == 2

    assert [(c.relation, c.full_name) for c in bundle.connections] == [("following", "박지성")]

    msg = bundle.messages[0].messages[0]
    assert [(m.local_path.split("/")[-1], m.media_type) for m in msg.media if m.local_path] == [
        ("noext_img", "image"),
        ("clip.mp4", "audio"),
    ]
    assert msg.share_text == "공유 설명"
    assert msg.is_missed_call is True

    acts_by_kind = {a.kind: a for a in bundle.activities}
    assert acts_by_kind["own_comment"].text == "축하해요!"
    assert acts_by_kind["own_comment"].media[0].local_path == "your_facebook_activity/posts/media/stickers_used/st1.png"
    assert acts_by_kind["group_comment"].group_name == "스터디모임"
    assert acts_by_kind["saved_item"].url == "https://dev.example.io/1"
