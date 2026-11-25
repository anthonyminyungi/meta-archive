"""Unit tests for archive delta patcher (`build/patcher.py`), Playwright sync parsers (`collect/sync.py`), and `meta-archive sync` CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from meta_archive_pipeline.build.indexer import build_sqlite_index, open_readonly_db, write_manifest_sha256
from meta_archive_pipeline.build.merger import materialize_archive, merge_all
from meta_archive_pipeline.build.patcher import (
    DmMediaRepair,
    SyncDelta,
    VideoAudioRepair,
    apply_sync_delta,
    load_archive_baseline,
)
from meta_archive_pipeline.cli import app
from meta_archive_pipeline.collect.sync import (
    build_ig_direct_user_map,
    parse_fb_messenger_dom_rows,
    parse_fb_messenger_timestamp,
    parse_ig_direct_item,
)
from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.schema.models import (
    Comment,
    Connection,
    LikesInfo,
    MediaItem,
    Message,
    MessageThread,
    Post,
    Profile,
    ProfileCounts,
    UserRef,
)
from meta_archive_pipeline.schema.paths import ArchivePaths


def _make_sample_archive(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    img = src / "media" / "posts" / "p1.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color=(40, 120, 200)).save(img, format="JPEG")

    ts = 1763643233  # 2025-11-20
    bundle = OfficialExportBundle(
        platform="instagram",
        root_dir=src,
        profile=Profile(
            platform="instagram",
            user_id="1621016495",
            username="yungis_",
            full_name="민윤기",
            counts=ProfileCounts(posts=1, followers=1),
        ),
        posts=[
            Post(
                platform="instagram",
                post_id="ig_p1",
                shortcode="SC_P1",
                timestamp_unix=ts,
                caption="기존 인스타 글",
                media=[
                    MediaItem(media_type="image", local_path="media/posts/p1.jpg"),
                    MediaItem(
                        media_type="video",
                        local_path="media/posts/p1.jpg",  # placeholder path
                        has_audio=False,
                    ),
                ],
                likes=LikesInfo(count=5),
                source="merged",
            ),
            # Unmerged FB duplicate pair (official empty stub + playwright post at exact same second)
            Post(
                platform="facebook",
                post_id="fb_official_dup1",
                post_type="post",
                timestamp_unix=ts - 500,
                caption="",
                activity_title="민윤기님이 상태를 업데이트했습니다.",
                source="official_export",
            ),
            Post(
                platform="facebook",
                post_id="3162337327234110",
                post_type="post",
                timestamp_unix=ts - 500,
                caption="",
                source="playwright",
            ),
        ],
        connections=[
            Connection(platform="instagram", relation="follower", username="alice", full_name="앨리스")
        ],
        messages=[
            MessageThread(
                platform="instagram",
                thread_id="inbox/baegjonghun_453420346051546",
                title="백종훈",
                participants=[UserRef(full_name="민윤기"), UserRef(full_name="백종훈")],
                messages=[
                    Message(
                        sender="민윤기",
                        timestamp_unix=ts - 100,
                        timestamp_ms=(ts - 100) * 1000,
                        created_at=datetime.fromtimestamp(ts - 100, tz=timezone.utc),
                        text="예전 메시지",
                    ),
                    Message(
                        sender="민윤기",
                        timestamp_unix=ts - 50,
                        timestamp_ms=(ts - 50) * 1000,
                        created_at=datetime.fromtimestamp(ts - 50, tz=timezone.utc),
                        text=None,
                        media=[
                            MediaItem(
                                media_type="image",
                                local_path=None,
                                original_uri="https://interncache-eag.fbcdn.net/expired.jpg",
                            )
                        ],
                    ),
                ],
            )
        ],
    )

    out_dir = tmp_path / "archive"
    # Build directly preserving both FB unmerged stubs so we test in-place dedup
    merged = merge_all(official_bundles=[bundle])
    # Re-inject both unmerged FB posts cleanly to mirror an archive built before the merger fix
    merged.posts = [
        p for p in merged.posts if p.platform != "facebook"
    ] + [
        Post(
            platform="facebook",
            post_id="fb_official_dup1",
            post_type="post",
            timestamp_unix=ts - 500,
            caption="",
            activity_title="민윤기님이 상태를 업데이트했습니다.",
            source="official_export",
        ),
        Post(
            platform="facebook",
            post_id="3162337327234110",
            post_type="post",
            timestamp_unix=ts - 500,
            caption="",
            source="playwright",
        ),
    ]
    materialize_archive(merged, out_dir, source_roots=[src], tz_offset="+09:00")
    build_sqlite_index(out_dir)
    write_manifest_sha256(out_dir)
    return out_dir


def test_parse_ig_direct_item_all_types() -> None:
    thread_obj = {
        "users": [{"pk": "999", "username": "jonghun", "full_name": "백종훈"}],
    }
    umap, participants = build_ig_direct_user_map(thread_obj, viewer_id="1621016495", viewer_name="민윤기")
    assert umap["1621016495"] == "민윤기"
    assert umap["999"] == "백종훈"
    assert len(participants) == 2

    raw_item = {
        "item_id": "mid_123",
        "user_id": "999",
        "timestamp": 1795587916000000,  # microseconds
        "item_type": "media",
        "text": "사진 보내요!",
        "media": {
            "id": "m_1",
            "image_versions2": {
                "candidates": [
                    {"url": "https://cdn.example/small.jpg", "width": 320, "height": 320},
                    {"url": "https://cdn.example/hd.jpg", "width": 1080, "height": 1080},
                ]
            },
        },
        "reactions": {"emojis": [{"sender_id": "1621016495", "emoji": "🔥"}]},
        "replied_to_message": {"item_id": "mid_prev"},
    }
    parsed = parse_ig_direct_item(raw_item, user_map=umap)
    assert parsed is not None
    assert parsed.message.sender == "백종훈"
    assert parsed.message.timestamp_ms == 1795587916000
    assert parsed.message.timestamp_unix == 1795587916
    assert parsed.message.text == "사진 보내요!"
    assert parsed.message.reply_to_id == "mid_prev"
    assert len(parsed.message.reactions) == 1
    assert parsed.message.reactions[0].actor == "민윤기"
    assert parsed.message.reactions[0].reaction == "🔥"
    assert parsed.media_downloads == [(0, "https://cdn.example/hd.jpg", "image")]

    # Voice message
    voice_item = {
        "item_id": "mid_voice",
        "user_id": "1621016495",
        "timestamp": 1795588000000000,
        "item_type": "voice_media",
        "voice_media": {"media": {"id": "v1", "audio": {"audio_src": "https://cdn.example/v.mp4", "duration": 4200}}},
    }
    vp = parse_ig_direct_item(voice_item, user_map=umap)
    assert vp is not None
    assert vp.message.sender == "민윤기"
    assert vp.message.media[0].media_type == "audio"
    assert vp.message.media[0].duration_sec == 4.2


def test_parse_fb_messenger_timestamps_and_dom_rows() -> None:
    ref = datetime(2026, 11, 25, 13, 0, tzinfo=timezone.utc)  # 22:00 KST
    dt1 = parse_fb_messenger_timestamp("2025년 3월 14일 오후 3:20", now=ref)
    assert dt1 is not None
    assert dt1.year == 2025 and dt1.month == 3 and dt1.day == 14

    dt2 = parse_fb_messenger_timestamp("어제 오전 9:05", now=ref)
    assert dt2 is not None
    assert dt2.day == 24

    dt3 = parse_fb_messenger_timestamp("2시간", now=ref)
    assert dt3 is not None
    assert int((ref - dt3).total_seconds()) == 7200

    dt4 = parse_fb_messenger_timestamp("Nov 22, 2026, 4:30 PM", now=ref)
    assert dt4 is not None
    assert dt4.year == 2026 and dt4.month == 11 and dt4.day == 22

    rows = [
        {"rawText": "2026년 11월 24일 오후 8:10", "dirTexts": [], "isRightAligned": False, "imgs": [], "vids": [], "links": []},
        {"rawText": "오랜만이야!", "dirTexts": ["오랜만이야!"], "isRightAligned": False, "imgs": [], "vids": [], "links": []},
        {"rawText": "잘 지내지?", "dirTexts": ["잘 지내지?"], "isRightAligned": True, "imgs": ["https://cdn.example/pic.jpg"], "vids": [], "links": []},
    ]
    parsed = parse_fb_messenger_dom_rows(
        rows,
        owner_name="민윤기",
        other_name="김철수",
        cutoff_ms=int(datetime(2024, 9, 13, tzinfo=timezone.utc).timestamp() * 1000),
        now=ref,
    )
    assert len(parsed) == 2
    assert parsed[0][0].sender == "김철수"
    assert parsed[0][0].text == "오랜만이야!"
    assert parsed[1][0].sender == "민윤기"
    assert parsed[1][0].text == "잘 지내지?"
    assert len(parsed[1][1]) == 1


def test_apply_sync_delta_dry_run_and_apply_full_roundtrip(tmp_path: Path) -> None:
    archive_dir = _make_sample_archive(tmp_path)
    baseline = load_archive_baseline(archive_dir)
    assert len(baseline.missing_dm_media("instagram")) == 1
    assert len(baseline.silent_post_videos("instagram")) == 1

    staging = tmp_path / "staging"
    staging.mkdir()
    repaired_img = staging / "repaired_dm.jpg"
    Image.new("RGB", (64, 64), color=(255, 80, 40)).save(repaired_img, format="JPEG")

    new_ts = 1795588000  # 2026-11-25
    delta = SyncDelta(
        staged_roots=[staging],
        posts=[
            # Updated comment on existing IG post
            Post(
                platform="instagram",
                post_id="ig_p1",
                shortcode="SC_P1",
                timestamp_unix=1763643233,
                likes=LikesInfo(count=7),
                comments=[
                    Comment(
                        comment_id="c_new_1",
                        author=UserRef(username="bob", full_name="밥"),
                        text="비활성화 풀었네!",
                        timestamp_unix=new_ts,
                    )
                ],
                source="playwright",
            )
        ],
        connections=[
            Connection(platform="instagram", relation="follower", username="alice", full_name="앨리스"),  # dup
            Connection(platform="instagram", relation="follower", username="charlie", full_name="찰리"),  # new
        ],
        message_threads=[
            # Appends to existing IG thread by title match
            MessageThread(
                platform="instagram",
                thread_id="340282366841710300949128199999",
                title="백종훈",
                messages=[
                    Message(
                        message_id="mid_dup",
                        sender="민윤기",
                        timestamp_unix=1763643233 - 100,
                        timestamp_ms=(1763643233 - 100) * 1000,
                        text="예전 메시지",
                    ),
                    Message(
                        message_id="mid_new_1",
                        sender="백종훈",
                        timestamp_unix=new_ts,
                        timestamp_ms=new_ts * 1000,
                        created_at=datetime.fromtimestamp(new_ts, tz=timezone.utc),
                        text="계정 지우기 전에 연락했어!",
                    ),
                ],
            ),
            # Brand new FB E2EE thread
            MessageThread(
                platform="facebook",
                thread_id="e2ee/888777666",
                title="김철수",
                is_e2ee=True,
                messages=[
                    Message(
                        sender="김철수",
                        timestamp_unix=new_ts,
                        timestamp_ms=new_ts * 1000,
                        created_at=datetime.fromtimestamp(new_ts, tz=timezone.utc),
                        text="암호화 채팅 테스트",
                    )
                ],
            ),
        ],
        dm_media_repairs=[
            DmMediaRepair(
                thread_id="inbox/baegjonghun_453420346051546",
                message_index=1,
                media_index=0,
                staged_file=repaired_img,
                width=64,
                height=64,
            )
        ],
        video_audio_repairs=[
            VideoAudioRepair(
                post_id="ig_p1",
                shortcode="SC_P1",
                media_index=1,
                status="confirmed_silent",
                detail="has_audio=false",
            )
        ],
    )

    # 1. Dry-run does not mutate archive
    posts_before = ArchivePaths(archive_dir).posts_json.read_text(encoding="utf-8")
    dry = apply_sync_delta(archive_dir, delta, apply=False, dedup_fb=True)
    assert dry.applied is False
    assert dry.fb_stubs_merged == 1
    assert dry.ig_new_messages == 1
    assert dry.ig_updated_threads == 1
    assert dry.fb_new_messages == 1
    assert dry.fb_new_threads == 1
    assert dry.dm_media_repaired == 1
    assert dry.videos_confirmed_silent == 1
    assert dry.posts_updated == 1
    assert dry.connections_added == 1
    assert ArchivePaths(archive_dir).posts_json.read_text(encoding="utf-8") == posts_before

    # 2. Apply mutates atomically, backs up outside archive/, rebuilds SQLite & manifest
    res = apply_sync_delta(archive_dir, delta, apply=True, dedup_fb=True)
    assert res.applied is True
    assert res.backup_dir is not None and res.backup_dir.exists()
    assert res.manifest_failures == []
    assert res.manifest_verified > 0

    after = load_archive_baseline(archive_dir)
    assert len(after.missing_dm_media("instagram")) == 0
    assert len([p for p in after.posts if p.platform == "facebook"]) == 1
    ig_p1 = next(p for p in after.posts if p.post_id == "ig_p1")
    assert ig_p1.likes.count == 7
    assert len(ig_p1.comments) == 1
    assert ig_p1.media[1].is_audio_muted is True

    conn = open_readonly_db(ArchivePaths(archive_dir).db_path)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM messages WHERE text LIKE '%암호화 채팅%'").fetchone()[0]
        assert cnt == 1
        dm_cnt = conn.execute("SELECT COUNT(*) FROM messages WHERE text LIKE '%계정 지우기 전에%'").fetchone()[0]
        assert dm_cnt == 1
    finally:
        conn.close()


def test_cli_sync_offline_only_dry_run(tmp_path: Path) -> None:
    archive_dir = _make_sample_archive(tmp_path)
    report_file = tmp_path / "sync_report.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "sync",
            "--archive-dir",
            str(archive_dir),
            "--offline-only",
            "--dry-run",
            "--report-out",
            str(report_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert report_file.exists()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["applied"] is False
    assert data["fb_stubs_merged"] == 1


def test_sync_config_and_response_collector_detach(tmp_path: Path) -> None:
    from meta_archive_pipeline.collect.cdp import ResponseCollector
    from meta_archive_pipeline.collect.sync import SyncConfig

    cfg = SyncConfig(archive_dir=tmp_path / "archive", staging_dir=tmp_path / "staging")
    assert cfg.cooldown.min_delay == 0.8
    assert cfg.cooldown.max_delay == 1.6

    events: list[str] = []

    class FakePage:
        def on(self, ev: str, fn: object) -> None:
            events.append(f"on:{ev}")

        def remove_listener(self, ev: str, fn: object) -> None:
            events.append(f"off:{ev}")

    col = ResponseCollector()
    col.attach(FakePage())
    col.detach()
    assert events == ["on:response", "off:response"]


def test_sync_helpers_with_fake_page_signatures(tmp_path: Path) -> None:
    from meta_archive_pipeline.collect.cdp import CooldownTracker
    from meta_archive_pipeline.collect.sync import (
        SyncConfig,
        _repair_ig_silent_videos,
        _sync_fb_delta,
        _sync_ig_profile_delta,
    )

    archive_dir = _make_sample_archive(tmp_path)
    baseline = load_archive_baseline(archive_dir)
    staging = tmp_path / "staging"
    staging.mkdir()

    cfg = SyncConfig(
        archive_dir=archive_dir,
        staging_dir=staging,
        cooldown=CooldownTracker(min_delay=0.0, max_delay=0.0, batch_size=0),
        wait_pin_sec=0,
    )

    ssr_ig_post = (
        '<html><script type="application/json">'
        + json.dumps(
            {
                "items": [
                    {
                        "code": "SC_P1",
                        "pk": "ig_p1",
                        "media_type": 2,
                        "is_video": True,
                        "has_audio": False,
                        "taken_at": 1763643233,
                        "user": {"pk": "1621016495", "username": "yungis_"},
                    }
                ]
            }
        )
        + "</script></html>"
    )

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/"
            self._listeners: list[object] = []

        def on(self, ev: str, fn: object) -> None:
            self._listeners.append(fn)

        def remove_listener(self, ev: str, fn: object) -> None:
            if fn in self._listeners:
                self._listeners.remove(fn)

        def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
            self.url = url

        def content(self) -> str:
            if "/p/SC_P1/" in self.url:
                return ssr_ig_post
            return "<html></html>"

        def inner_text(self, sel: str) -> str:
            return ""

        def evaluate(self, script: str, arg: object = None) -> object:
            if "fetch(" in script:
                return {"status": 200, "data": {"reels_media": [], "users": []}}
            return []

    page = FakePage()
    delta = SyncDelta(staged_roots=[staging])
    _repair_ig_silent_videos(page, None, baseline, cfg, delta)
    assert len(delta.video_audio_repairs) == 1
    assert delta.video_audio_repairs[0].status == "confirmed_silent"

    _sync_ig_profile_delta(page, None, baseline, cfg, delta)
    _sync_fb_delta(page, None, baseline, cfg, delta)


def test_parse_ig_direct_dom_leaves_and_korean_weekday() -> None:
    from meta_archive_pipeline.collect.sync import parse_ig_direct_dom_leaves

    # Thu 2026-11-26 00:35 KST = 2026-11-25 15:35 UTC
    ref = datetime(2026, 11, 25, 15, 35, tzinfo=timezone.utc)
    dt_wed = parse_fb_messenger_timestamp("(수) 오후 9:24", now=ref)
    assert dt_wed is not None
    assert dt_wed.astimezone(timezone(datetime.now().astimezone().utcoffset())).day == 25

    leaves = [
        {"tag": "DIV", "text": "헤더 이전 오래된 메시지", "leftPct": 3, "rightPct": 20},
        {"tag": "SPAN", "text": "(수) 오후 9:24", "leftPct": 1, "rightPct": 99},
        {"tag": "SPAN", "text": "오리너구리", "leftPct": 3, "rightPct": 6},
        {"tag": "IMG", "src": "https://scontent.cdninstagram.com/v/t51.2885-19/avatar.jpg", "w": 150, "h": 150, "leftPct": 3, "rightPct": 5},
        {"tag": "A", "href": "/artart.today/", "text": "artart.today", "leftPct": 6, "rightPct": 10},
        {"tag": "SPAN", "text": "artart.today", "leftPct": 6, "rightPct": 10},
        {"tag": "IMG", "src": "https://scontent.cdninstagram.com/v/t51.82787-15/reel_thumb.jpg", "w": 1080, "h": 1920, "leftPct": 3, "rightPct": 13},
        {"tag": "DIV", "text": "재능 지리네", "leftPct": 3, "rightPct": 7},
        {"tag": "DIV", "text": "인정ㅋㅋ", "leftPct": 88, "rightPct": 98},
        {"tag": "SPAN", "text": "백종훈님이 확인함", "leftPct": 93, "rightPct": 98},
    ]
    parsed = parse_ig_direct_dom_leaves(
        leaves,
        viewer_name="민윤기",
        thread_title="춫넌",
        is_group=True,
        cutoff_ms=int(datetime(2026, 11, 20, 12, 53, tzinfo=timezone.utc).timestamp() * 1000),
        now=ref,
    )
    assert len(parsed) == 3
    assert parsed[0][0].sender == "오리너구리"
    assert parsed[0][0].share_owner == "artart.today"
    assert parsed[0][0].shared_link == "https://www.instagram.com/artart.today/"
    assert parsed[0][0].media == []
    assert parsed[0][1] == []
    assert parsed[1][0].sender == "오리너구리"
    assert parsed[1][0].text == "재능 지리네"
    assert parsed[2][0].sender == "민윤기"
    assert parsed[2][0].text == "인정ㅋㅋ"
