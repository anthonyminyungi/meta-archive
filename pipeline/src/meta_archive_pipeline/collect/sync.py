"""Playwright CDP incremental sync & targeted gap repair (IG DM, silent videos, FB Messenger/E2EE, IG/FB deltas)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from meta_archive_pipeline.build.patcher import (
    ArchiveBaseline,
    DmMediaRepair,
    MissingDmMediaTarget,
    SilentVideoTarget,
    SyncDelta,
    VideoAudioRepair,
    load_archive_baseline,
)
from meta_archive_pipeline.collect.cdp import (
    DEFAULT_CDP_HOST,
    DEFAULT_CDP_PORT,
    CooldownTracker,
    ResponseCollector,
    SafetyStopError,
    check_page_safety,
    connect_cdp,
    extract_ssr_json_payloads,
)
from meta_archive_pipeline.collect.fb_collector import (
    extract_fb_friends_from_payloads,
    extract_fb_posts_from_payloads,
)
from meta_archive_pipeline.collect.ig_collector import (
    _best_image_candidate,
    _media_candidate_from_node,
    _walk_dicts,
    download_post_media,
    extract_ig_dom_shortcodes,
    extract_ig_highlights_from_payloads,
    extract_ig_post_from_payloads,
    extract_ig_profile_from_payloads,
    extract_ig_shortcodes_from_payloads,
    parse_dash_manifest,
)
from meta_archive_pipeline.schema.models import (
    Connection,
    MediaItem,
    Message,
    MessageReaction,
    MessageThread,
    Post,
    Story,
    UserRef,
)
from meta_archive_pipeline.utils.fs import to_long_path
from meta_archive_pipeline.utils.media import (
    download_media_via_context,
    inspect_mp4,
    mux_dash_streams,
    verify_image,
)

_IG_APP_ID = "936619743392459"
_KST = timezone(timedelta(hours=9))

_KO_FULL_DT_RE = re.compile(
    r"(?:(?P<year>\d{4})\s*년\s*)?(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
    r"(?:\s*(?:\([월화수목금토일]\)|[월화수목금토일]요일))?"
    r"(?:\s*(?P<ampm>오전|오후)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_KO_DOT_DT_RE = re.compile(
    r"(?P<year>\d{4})\.\s*(?P<month>\d{1,2})\.\s*(?P<day>\d{1,2})\.?"
    r"(?:\s*(?P<ampm>오전|오후)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_KO_REL_TIME_RE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>분|시간|일|주|개월|년)$")
_KO_WEEKDAY_TIME_RE = re.compile(
    r"^\(?(?P<wd>[월화수목금토일])\)?(?:요일)?\s*(?P<ampm>오전|오후)?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})$"
)
_KO_WEEKDAY_MAP = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_EN_REL_TIME_RE = re.compile(r"^(?P<num>\d+)\s*(?P<unit>m|min|h|hr|d|w|mo|y)$", re.IGNORECASE)
_TIME_ONLY_RE = re.compile(r"^(?:(?P<ampm>오전|오후|AM|PM)\s*)?(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*(?P<ampm2>AM|PM))?$", re.IGNORECASE)
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_EN_DATE_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?"
    r"(?:,?\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)?)?",
    re.IGNORECASE,
)


@dataclass
class SyncConfig:
    """Configuration for Playwright CDP incremental sync & gap repair."""

    archive_dir: Path
    staging_dir: Path
    cdp_url: str = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}"
    sync_ig: bool = True
    sync_fb: bool = True
    sync_ig_dm: bool = True
    repair_ig_dm_media: bool = True
    repair_ig_silent_videos: bool = True
    sync_ig_profile: bool = True
    sync_fb_messenger: bool = True
    sync_fb_timeline: bool = True
    recent_posts_check_limit: int = 10
    max_inbox_pages: int = 15
    max_thread_pages: int = 40
    max_repair_thread_pages: int = 150
    max_fb_threads: int = 25
    wait_pin_sec: int = 90
    cooldown: CooldownTracker = field(
        default_factory=lambda: CooldownTracker(min_delay=0.8, max_delay=1.6, batch_size=50)
    )


def _norm_ts_ms(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 0
    if val <= 0:
        return 0
    if val > 10**14:  # microseconds
        return val // 1000
    if val > 10**11:  # milliseconds
        return val
    return val * 1000  # seconds


def _ms_to_dt(ms: int) -> datetime | None:
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _best_url_from_image_versions(obj: Any) -> tuple[str | None, int | None, int | None]:
    if not isinstance(obj, dict):
        return None, None, None
    cands = obj.get("candidates")
    if not isinstance(cands, list):
        return None, None, None
    best_url = None
    best_area = -1
    best_dims: tuple[int | None, int | None] = (None, None)
    for c in cands:
        if not isinstance(c, dict) or not isinstance(c.get("url"), str):
            continue
        w = int(c["width"]) if isinstance(c.get("width"), (int, float)) else None
        h = int(c["height"]) if isinstance(c.get("height"), (int, float)) else None
        area = (w or 0) * (h or 0)
        if area > best_area:
            best_area = area
            best_url = c["url"]
            best_dims = (w, h)
    return best_url, best_dims[0], best_dims[1]


def _best_url_from_video_versions(versions: Any) -> tuple[str | None, int | None, int | None]:
    if not isinstance(versions, list):
        return None, None, None
    best_url = None
    best_area = -1
    best_dims: tuple[int | None, int | None] = (None, None)
    for v in versions:
        if not isinstance(v, dict) or not isinstance(v.get("url"), str):
            continue
        w = int(v["width"]) if isinstance(v.get("width"), (int, float)) else None
        h = int(v["height"]) if isinstance(v.get("height"), (int, float)) else None
        area = (w or 0) * (h or 0)
        if area > best_area:
            best_area = area
            best_url = v["url"]
            best_dims = (w, h)
    return best_url, best_dims[0], best_dims[1]


@dataclass
class ParsedDirectItem:
    """A parsed Instagram Direct message item plus any downloadable remote media URLs."""

    message: Message
    media_downloads: list[tuple[int, str, str]] = field(default_factory=list)  # (media_idx, url, media_type)


def parse_ig_direct_item(
    item: dict[str, Any],
    *,
    user_map: dict[str, str],
) -> ParsedDirectItem | None:
    """Parse one raw Instagram `/api/v1/direct_v2/` thread item into a canonical `Message`."""
    if not isinstance(item, dict):
        return None

    item_id = str(item.get("item_id") or item.get("client_context") or "") or None
    ts_ms = _norm_ts_ms(item.get("timestamp"))
    ts_unix = ts_ms // 1000 if ts_ms else None
    uid = str(item.get("user_id") or "")
    sender = user_map.get(uid) or uid or "Instagram User"

    item_type = str(item.get("item_type") or "")
    text: str | None = item.get("text") if isinstance(item.get("text"), str) else None
    shared_link: str | None = None
    share_text: str | None = None
    share_owner: str | None = None
    is_unsent = False
    is_missed_call = False
    call_duration: int | None = None

    media_items: list[MediaItem] = []
    media_downloads: list[tuple[int, str, str]] = []

    def _add_media_dict(media_dict: Any) -> None:
        if not isinstance(media_dict, dict):
            return
        mid = str(media_dict.get("id") or media_dict.get("pk") or "") or None
        vid_url, vw, vh = _best_url_from_video_versions(media_dict.get("video_versions"))
        if vid_url:
            idx = len(media_items)
            media_items.append(
                MediaItem(
                    media_id=mid,
                    media_type="video",
                    original_uri=vid_url,
                    width=vw,
                    height=vh,
                )
            )
            media_downloads.append((idx, vid_url, "video"))
            return
        img_url, iw, ih = _best_url_from_image_versions(media_dict.get("image_versions2"))
        if img_url:
            idx = len(media_items)
            media_items.append(
                MediaItem(
                    media_id=mid,
                    media_type="image",
                    original_uri=img_url,
                    width=iw,
                    height=ih,
                )
            )
            media_downloads.append((idx, img_url, "image"))

    if item_type == "media" or isinstance(item.get("media"), dict):
        _add_media_dict(item.get("media"))

    if isinstance(item.get("visual_media"), dict):
        _add_media_dict(item["visual_media"].get("media"))
    if isinstance(item.get("raven_media"), dict):
        _add_media_dict(item["raven_media"])

    if isinstance(item.get("voice_media"), dict):
        vm = item["voice_media"].get("media")
        if isinstance(vm, dict) and isinstance(vm.get("audio"), dict):
            audio_src = vm["audio"].get("audio_src")
            dur = vm["audio"].get("duration")
            if isinstance(audio_src, str) and audio_src:
                idx = len(media_items)
                media_items.append(
                    MediaItem(
                        media_id=str(vm.get("id") or "") or None,
                        media_type="audio",
                        original_uri=audio_src,
                        duration_sec=float(dur) / 1000.0 if isinstance(dur, (int, float)) and dur > 100 else (float(dur) if isinstance(dur, (int, float)) else None),
                    )
                )
                media_downloads.append((idx, audio_src, "audio"))

    if isinstance(item.get("animated_media"), dict):
        imgs = item["animated_media"].get("images")
        if isinstance(imgs, dict):
            fh = imgs.get("fixed_height")
            if isinstance(fh, dict) and isinstance(fh.get("url"), str):
                idx = len(media_items)
                media_items.append(MediaItem(media_type="image", original_uri=fh["url"]))
                media_downloads.append((idx, fh["url"], "image"))

    if isinstance(item.get("link"), dict):
        link_obj = item["link"]
        if not text and isinstance(link_obj.get("text"), str):
            text = link_obj["text"]
        lctx = link_obj.get("link_context")
        if isinstance(lctx, dict):
            shared_link = lctx.get("link_url") or shared_link
            share_text = lctx.get("link_title") or lctx.get("link_summary") or share_text

    for share_key in ("clip", "media_share", "reel_share", "story_share", "felix_share"):
        sobj = item.get(share_key)
        if not isinstance(sobj, dict):
            continue
        inner = sobj.get("clip") or sobj.get("media") or sobj.get("video") or sobj
        if isinstance(sobj.get("text"), str) and sobj["text"] and not text:
            text = sobj["text"]
        if isinstance(inner, dict):
            sc = inner.get("code") or inner.get("shortcode")
            if isinstance(sc, str) and sc and not shared_link:
                prefix = "reel" if share_key == "clip" else "p"
                shared_link = f"https://www.instagram.com/{prefix}/{sc}/"
            owner = inner.get("user")
            if isinstance(owner, dict) and not share_owner:
                share_owner = owner.get("username") or owner.get("full_name")
            cap = inner.get("caption")
            if isinstance(cap, dict) and isinstance(cap.get("text"), str) and not share_text:
                share_text = cap["text"]

    xma_list = item.get("xma_media_share") or item.get("xma_share")
    if isinstance(xma_list, list) and xma_list and isinstance(xma_list[0], dict):
        x0 = xma_list[0]
        shared_link = shared_link or x0.get("target_url")
        share_text = share_text or x0.get("title_text") or x0.get("subtitle_text")
        share_owner = share_owner or x0.get("header_title_text")

    if isinstance(item.get("action_log"), dict) and not text:
        desc = item["action_log"].get("description")
        if isinstance(desc, str) and desc:
            text = desc

    if item_type == "placeholder" and isinstance(item.get("placeholder"), dict):
        pm = item["placeholder"].get("message")
        if isinstance(pm, str) and not text:
            text = pm

    if isinstance(item.get("video_call_event"), dict):
        vce = item["video_call_event"]
        desc = vce.get("description")
        if isinstance(desc, str) and not text:
            text = desc
        action = str(vce.get("action") or "").lower()
        if "missed" in action:
            is_missed_call = True
        dur = vce.get("call_duration")
        if isinstance(dur, (int, float)) and dur > 0:
            call_duration = int(dur)

    reactions: list[MessageReaction] = []
    raw_reacts = item.get("reactions")
    if isinstance(raw_reacts, dict):
        for rkey in ("emojis", "likes"):
            rlist = raw_reacts.get(rkey)
            if not isinstance(rlist, list):
                continue
            for r in rlist:
                if not isinstance(r, dict):
                    continue
                actor = user_map.get(str(r.get("sender_id") or "")) or str(r.get("sender_id") or "")
                emoji = str(r.get("emoji") or "❤️")
                if actor:
                    reactions.append(MessageReaction(actor=actor, reaction=emoji))

    reply_to_id: str | None = None
    if isinstance(item.get("replied_to_message"), dict):
        rid = item["replied_to_message"].get("item_id")
        if rid:
            reply_to_id = str(rid)

    if not text and not media_items and not shared_link and not is_missed_call:
        return None

    msg = Message(
        message_id=item_id,
        sender=sender,
        timestamp_unix=ts_unix,
        timestamp_ms=ts_ms or None,
        created_at=_ms_to_dt(ts_ms),
        text=text,
        media=media_items,
        reactions=reactions,
        reply_to_id=reply_to_id,
        shared_link=shared_link,
        share_text=share_text,
        share_owner=share_owner,
        is_unsent=is_unsent,
        is_missed_call=is_missed_call,
        call_duration=call_duration,
    )
    return ParsedDirectItem(message=msg, media_downloads=media_downloads)


def build_ig_direct_user_map(
    thread_obj: dict[str, Any],
    *,
    viewer_id: str | None = None,
    viewer_name: str | None = None,
) -> tuple[dict[str, str], list[UserRef]]:
    """Build `user_id -> display_name` map and `participants` list from a Direct thread payload."""
    user_map: dict[str, str] = {}
    participants: list[UserRef] = []
    if viewer_id and viewer_name:
        user_map[str(viewer_id)] = viewer_name
        participants.append(UserRef(user_id=str(viewer_id), full_name=viewer_name))

    for u in thread_obj.get("users") or []:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("pk") or u.get("id") or "")
        uname = u.get("username") if isinstance(u.get("username"), str) else None
        fname = u.get("full_name") if isinstance(u.get("full_name"), str) and u.get("full_name") else None
        display = fname or uname or uid
        if uid and display:
            user_map[uid] = display
        participants.append(UserRef(user_id=uid or None, username=uname, full_name=fname or uname))

    return user_map, participants


def parse_fb_messenger_timestamp(
    label: str,
    *,
    now: datetime | None = None,
    tz: timezone = _KST,
) -> datetime | None:
    """Parse Korean/English Facebook Messenger date headers or relative timestamps into UTC datetime."""
    text = " ".join((label or "").split()).strip()
    if not text:
        return None
    ref = (now or datetime.now(timezone.utc)).astimezone(tz)

    def _to_24h(hour: int, ampm: str | None) -> int:
        if not ampm:
            return hour
        low = ampm.lower()
        if low in ("오후", "pm") and hour < 12:
            return hour + 12
        if low in ("오전", "am") and hour == 12:
            return 0
        return hour

    # Relative short labels: "2분", "3시간", "4일", "2주", "5m", "3h", "2d"
    m_rel = _KO_REL_TIME_RE.match(text)
    if m_rel:
        num = int(m_rel.group("num"))
        unit = m_rel.group("unit")
        deltas = {
            "분": timedelta(minutes=num),
            "시간": timedelta(hours=num),
            "일": timedelta(days=num),
            "주": timedelta(weeks=num),
            "개월": timedelta(days=num * 30),
            "년": timedelta(days=num * 365),
        }
        return (ref - deltas[unit]).astimezone(timezone.utc)

    m_erel = _EN_REL_TIME_RE.match(text)
    if m_erel:
        num = int(m_erel.group("num"))
        unit = m_erel.group("unit").lower()
        deltas_en = {
            "m": timedelta(minutes=num),
            "min": timedelta(minutes=num),
            "h": timedelta(hours=num),
            "hr": timedelta(hours=num),
            "d": timedelta(days=num),
            "w": timedelta(weeks=num),
            "mo": timedelta(days=num * 30),
            "y": timedelta(days=num * 365),
        }
        return (ref - deltas_en[unit]).astimezone(timezone.utc)

    # Korean weekday + time: "(수) 오전 7:44", "화요일 오후 9:24"
    m_wd = _KO_WEEKDAY_TIME_RE.match(text)
    if m_wd:
        target_wd = _KO_WEEKDAY_MAP[m_wd.group("wd")]
        days_back = (ref.weekday() - target_wd) % 7
        hour = _to_24h(int(m_wd.group("hour")), m_wd.group("ampm"))
        minute = int(m_wd.group("minute"))
        cand = (ref - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand > ref + timedelta(minutes=5):
            cand -= timedelta(days=7)
        return cand.astimezone(timezone.utc)

    # Korean full date: "2025년 11월 24일 오후 3:15" or "11월 24일"
    m_ko = _KO_FULL_DT_RE.search(text)
    if m_ko:
        year = int(m_ko.group("year")) if m_ko.group("year") else ref.year
        month = int(m_ko.group("month"))
        day = int(m_ko.group("day"))
        hour = _to_24h(int(m_ko.group("hour")), m_ko.group("ampm")) if m_ko.group("hour") else 12
        minute = int(m_ko.group("minute")) if m_ko.group("minute") else 0
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=tz)
            if not m_ko.group("year") and dt > ref + timedelta(days=2):
                dt = dt.replace(year=year - 1)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    # Korean dotted date: "2025. 11. 24. 오후 3:15"
    m_dot = _KO_DOT_DT_RE.search(text)
    if m_dot:
        year = int(m_dot.group("year"))
        month = int(m_dot.group("month"))
        day = int(m_dot.group("day"))
        hour = _to_24h(int(m_dot.group("hour")), m_dot.group("ampm")) if m_dot.group("hour") else 12
        minute = int(m_dot.group("minute")) if m_dot.group("minute") else 0
        try:
            return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            return None

    # English date: "Nov 24, 2025, 3:15 PM"
    m_en = _EN_DATE_RE.search(text)
    if m_en:
        year = int(m_en.group("year")) if m_en.group("year") else ref.year
        month = _EN_MONTHS[m_en.group("mon").lower()[:3]]
        day = int(m_en.group("day"))
        hour = _to_24h(int(m_en.group("hour")), m_en.group("ampm")) if m_en.group("hour") else 12
        minute = int(m_en.group("minute")) if m_en.group("minute") else 0
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=tz)
            if not m_en.group("year") and dt > ref + timedelta(days=2):
                dt = dt.replace(year=year - 1)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None

    # "어제 오후 3:15" / "Yesterday 3:15 PM"
    low = text.lower()
    if text.startswith("어제") or low.startswith("yesterday"):
        rest = re.sub(r"^(?:어제|yesterday)\s*", "", text, flags=re.IGNORECASE).strip()
        base = (ref - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        mt = _TIME_ONLY_RE.match(rest)
        if mt:
            ampm = mt.group("ampm") or mt.group("ampm2")
            base = base.replace(hour=_to_24h(int(mt.group("hour")), ampm), minute=int(mt.group("minute")))
        return base.astimezone(timezone.utc)

    # Time only (today): "오후 3:15" / "3:15 PM"
    mt = _TIME_ONLY_RE.match(text)
    if mt:
        ampm = mt.group("ampm") or mt.group("ampm2")
        dt = ref.replace(
            hour=_to_24h(int(mt.group("hour")), ampm),
            minute=int(mt.group("minute")),
            second=0,
            microsecond=0,
        )
        return dt.astimezone(timezone.utc)

    return None


def _page_fetch_json(
    page: Any,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """Execute a same-origin `fetch()` inside the logged-in browser page and return `(status, json_or_None)`."""
    result = page.evaluate(
        """async ({ url, headers }) => {
            try {
                const resp = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: headers || {},
                });
                const text = await resp.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return { status: resp.status, data, text: text.slice(0, 400) };
            } catch (err) {
                return { status: 0, data: null, text: String(err) };
            }
        }""",
        {"url": url, "headers": headers or {}},
    )
    if not isinstance(result, dict):
        return 0, None
    status = int(result.get("status") or 0)
    if status == 429:
        raise SafetyStopError("rate_limited", url, status=429, detail="HTTP 429 from in-page fetch")
    return status, result.get("data")


def _ig_api_headers(page: Any) -> dict[str, str]:
    csrf = ""
    try:
        csrf = page.evaluate(
            """() => {
                const m = document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/);
                return m ? decodeURIComponent(m[1]) : '';
            }"""
        ) or ""
    except Exception:
        csrf = ""
    hdrs = {
        "x-ig-app-id": _IG_APP_ID,
        "x-requested-with": "XMLHttpRequest",
        "accept": "*/*",
    }
    if csrf:
        hdrs["x-csrftoken"] = csrf
    return hdrs


def _download_staged_asset(
    request_context: Any,
    url: str,
    dest_no_ext: Path,
    *,
    referer: str,
    media_type: str,
) -> Path | None:
    ext_map = {"image": ".jpg", "video": ".mp4", "audio": ".mp4"}
    default_ext = ext_map.get(media_type, ".bin")
    parsed_ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if parsed_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".m4a", ".aac", ".ogg", ".wav"):
        ext = parsed_ext
    else:
        ext = default_ext
    dest = dest_no_ext.with_suffix(ext)
    try:
        download_media_via_context(
            request_context,
            url,
            dest,
            headers={"Referer": referer},
        )
        if media_type == "image":
            chk = verify_image(dest)
            if not chk.ok:
                try:
                    os.remove(to_long_path(dest))
                except OSError:
                    pass
                return None
        return dest
    except Exception:
        return None


_IG_DM_INBOX_LIST_JS = """() => {
  const panes = Array.from(document.querySelectorAll('div')).filter(el => {
    const oy = getComputedStyle(el).overflowY;
    const r = el.getBoundingClientRect();
    return (oy === 'auto' || oy === 'scroll') && r.left < 300 && r.width > 180 && r.width < 520 && el.clientHeight > 300;
  });
  const inbox = panes[0] || document.body;
  const rows = Array.from(inbox.querySelectorAll('[role="listitem"], [role="button"]'));
  const seen = new Set();
  const out = [];
  for (const el of rows) {
    const raw = (el.innerText || '').trim();
    if (!raw || raw.length > 260 || !raw.includes('\\n')) continue;
    const lines = raw.split('\\n').map(s => s.trim()).filter(s => s && s !== '·' && s !== '\\u00a0');
    if (lines.length < 2) continue;
    if (lines[0].includes('노래를 남겨주세요') || lines[1] === '내 메모') continue;
    const title = lines[0];
    if (seen.has(title)) continue;
    seen.add(title);
    const timeLabel = lines.length >= 3 ? lines[lines.length - 1] : '';
    const preview = lines.slice(1, lines.length >= 3 ? -1 : undefined).join(' ');
    out.push({ title, preview, timeLabel, lines });
  }
  return out;
}"""

_IG_DM_PANE_LEAVES_JS = """() => {
  const panes = Array.from(document.querySelectorAll('div')).filter(el => {
    const oy = getComputedStyle(el).overflowY;
    const r = el.getBoundingClientRect();
    return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20 && r.left > 280 && r.width > 360;
  });
  const pane = panes[panes.length - 1];
  if (!pane) return { found: false, leaves: [], scrollTop: 0, scrollHeight: 0 };
  const pr = pane.getBoundingClientRect();
  const nodes = Array.from(pane.querySelectorAll('[dir="auto"], a[href], img[src], video[src], video source[src], audio[src]'));
  const leaves = [];
  for (const el of nodes) {
    const tag = el.tagName;
    if (tag === 'IMG') {
      const w = el.naturalWidth || el.width || 0;
      const h = el.naturalHeight || el.height || 0;
      if (w < 40 || h < 40) continue;
    } else if (tag === 'A') {
      if (!el.getAttribute('href')) continue;
    } else if (el.getAttribute('dir') === 'auto') {
      if (el.querySelector('[dir="auto"]')) continue;
    }
    const r = el.getBoundingClientRect();
    leaves.push({
      tag,
      dir: el.getAttribute('dir') || null,
      href: el.getAttribute('href') || null,
      src: (tag === 'IMG' || tag === 'VIDEO' || tag === 'SOURCE' || tag === 'AUDIO') ? (el.src || el.getAttribute('src') || null) : null,
      w: tag === 'IMG' ? (el.naturalWidth || el.width || 0) : null,
      h: tag === 'IMG' ? (el.naturalHeight || el.height || 0) : null,
      text: (tag === 'IMG' || tag === 'VIDEO' || tag === 'SOURCE' || tag === 'AUDIO') ? null : (el.innerText || '').trim(),
      leftPct: pr.width > 0 ? Math.round(((r.left - pr.left) / pr.width) * 100) : 0,
      rightPct: pr.width > 0 ? Math.round(((r.right - pr.left) / pr.width) * 100) : 0,
      top: Math.round(r.top),
    });
  }
  return {
    found: true,
    leaves,
    scrollTop: Math.round(pane.scrollTop),
    scrollHeight: Math.round(pane.scrollHeight),
    clientHeight: Math.round(pane.clientHeight),
  };
}"""

_IG_DM_RECEIPT_RE = re.compile(
    r"^(?:확인한 시간:|.*님이 확인함$|전송됨$|읽음$|보냄$|Seen\b|Sent\b|Delivered\b|메시지를 좋아합니다$)"
)
_IG_PROFILE_HREF_RE = re.compile(r"^/([A-Za-z0-9._]+)/?$")
_IG_ASSET_ID_RE = re.compile(r"(\d{6,}_\d{6,}_\d{6,}_[a-z])")


def _clean_ig_dm_title(raw_title: str) -> str:
    s = " ".join((raw_title or "").split()).strip()
    if s.endswith("님") and " 외 " not in s and len(s) >= 2:
        return s[:-1].strip()
    return s


def _is_ig_avatar_url(url: str, width: int | None = None) -> bool:
    low = (url or "").lower()
    if "-19/" in low or "t1.30497-1" in low or "static.cdninstagram.com" in low or "emoji" in low:
        return True
    if width is not None and width < 250:
        return True
    return False


def parse_ig_direct_dom_leaves(
    leaves: Sequence[dict[str, Any]],
    *,
    viewer_name: str,
    thread_title: str,
    is_group: bool = False,
    cutoff_ms: int = 0,
    now: datetime | None = None,
) -> list[tuple[Message, list[tuple[int, str, str]]]]:
    """Parse 2026 Instagram Direct DOM leaf nodes into ordered canonical `Message` objects."""
    clean_title = _clean_ig_dm_title(thread_title)
    current_dt: datetime | None = None
    seq_in_minute = 0
    current_sender: str | None = None
    pending_share_owner: str | None = None
    pending_share_link: str | None = None
    out: list[tuple[Message, list[tuple[int, str, str]]]] = []

    def _next_dt() -> tuple[datetime | None, int]:
        nonlocal seq_in_minute
        if current_dt is None:
            return None, 0
        dt = current_dt + timedelta(seconds=seq_in_minute)
        seq_in_minute += 1
        return dt, int(dt.timestamp() * 1000)

    i = 0
    n = len(leaves)
    while i < n:
        lf = leaves[i]
        tag = str(lf.get("tag") or "").upper()
        text = str(lf.get("text") or "").strip()
        href = str(lf.get("href") or "").strip()
        src = str(lf.get("src") or "").strip()
        left_pct = int(lf.get("leftPct") or 0)
        right_pct = int(lf.get("rightPct") or 0)
        w = lf.get("w")

        # 1. Centered full-width timestamp header
        if tag == "SPAN" and text and left_pct <= 5 and right_pct >= 90:
            pdt = parse_fb_messenger_timestamp(text, now=now)
            if pdt is not None:
                current_dt = pdt
                seq_in_minute = 0
                pending_share_owner = None
                pending_share_link = None
                i += 1
                continue

        # 2. Skip delivery / read receipts
        if tag == "SPAN" and text and _IG_DM_RECEIPT_RE.match(text):
            i += 1
            continue

        # 3. Link inside shared reel/post header (e.g. <a href="/artart.today/">artart.today</a>)
        if tag == "A" and href:
            if href.startswith(("/reel/", "/p/")):
                pending_share_link = f"https://www.instagram.com{href}"
            elif text:
                m_prof = _IG_PROFILE_HREF_RE.match(href)
                if m_prof and m_prof.group(1) == text:
                    pending_share_owner = text
            i += 1
            continue

        # 4. Left-aligned SPAN: either shared-card author duplicate or group chat sender name
        if tag == "SPAN" and text:
            if pending_share_owner and text == pending_share_owner:
                i += 1
                continue
            if is_group and left_pct <= 10 and right_pct < 60 and len(text) <= 40:
                current_sender = text
            i += 1
            continue

        # 5. Text bubble (DIV[dir="auto"])
        if tag == "DIV" and text:
            if _IG_DM_RECEIPT_RE.match(text):
                i += 1
                continue
            is_mine = left_pct >= 45 and right_pct >= 88
            sender = viewer_name if is_mine else (current_sender if is_group and current_sender else clean_title)
            dt, ts_ms = _next_dt()
            if not (cutoff_ms and ts_ms <= cutoff_ms):
                out.append(
                    (
                        Message(
                            sender=sender,
                            timestamp_unix=(ts_ms // 1000) if ts_ms else None,
                            timestamp_ms=ts_ms or None,
                            created_at=dt,
                            text=text,
                        ),
                        [],
                    )
                )
            pending_share_owner = None
            pending_share_link = None
            i += 1
            continue

        # 6. Media attachment (non-avatar IMG, VIDEO, AUDIO)
        # Shared reel/post cards (preceded by a profile/post link header) match official exports:
        # keep share metadata/link without saving third-party reel thumbnails as DM photo media.
        if tag == "IMG" and src and not _is_ig_avatar_url(src, w if isinstance(w, int) else None):
            is_mine = left_pct >= 45 and right_pct >= 88
            sender = viewer_name if is_mine else (current_sender if is_group and current_sender else clean_title)
            dt, ts_ms = _next_dt()
            if not (cutoff_ms and ts_ms <= cutoff_ms):
                if pending_share_owner or pending_share_link:
                    fallback_link = (
                        pending_share_link
                        or (f"https://www.instagram.com/{pending_share_owner}/" if pending_share_owner else None)
                    )
                    msg = Message(
                        sender=sender,
                        timestamp_unix=(ts_ms // 1000) if ts_ms else None,
                        timestamp_ms=ts_ms or None,
                        created_at=dt,
                        text=f"{sender}님이 첨부 파일을 보냈습니다.",
                        media=[],
                        shared_link=fallback_link,
                        share_owner=pending_share_owner,
                    )
                    out.append((msg, []))
                else:
                    media_item = MediaItem(
                        media_type="image",
                        original_uri=src,
                        width=int(w) if isinstance(w, (int, float)) else None,
                        height=int(lf["h"]) if isinstance(lf.get("h"), (int, float)) else None,
                    )
                    msg = Message(
                        sender=sender,
                        timestamp_unix=(ts_ms // 1000) if ts_ms else None,
                        timestamp_ms=ts_ms or None,
                        created_at=dt,
                        text=None,
                        media=[media_item],
                    )
                    out.append((msg, [(0, src, "image")]))
            pending_share_owner = None
            pending_share_link = None
            i += 1
            continue

        if tag in ("VIDEO", "SOURCE", "AUDIO") and src and src.startswith("http"):
            mtype = "audio" if tag == "AUDIO" else "video"
            is_mine = left_pct >= 45 and right_pct >= 88
            sender = viewer_name if is_mine else (current_sender if is_group and current_sender else clean_title)
            dt, ts_ms = _next_dt()
            if not (cutoff_ms and ts_ms <= cutoff_ms):
                msg = Message(
                    sender=sender,
                    timestamp_unix=(ts_ms // 1000) if ts_ms else None,
                    timestamp_ms=ts_ms or None,
                    created_at=dt,
                    media=[MediaItem(media_type=mtype, original_uri=src)],
                )
                out.append((msg, [(0, src, mtype)]))
            i += 1
            continue

        i += 1

    return out


def _scroll_ig_pane_up(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                  const panes = Array.from(document.querySelectorAll('div')).filter(el => {
                    const oy = getComputedStyle(el).overflowY;
                    const r = el.getBoundingClientRect();
                    return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20 && r.left > 280 && r.width > 360;
                  });
                  const pane = panes[panes.length - 1];
                  if (!pane) return false;
                  const before = pane.scrollTop;
                  pane.scrollTop = Math.max(0, pane.scrollTop - Math.max(600, pane.clientHeight));
                  pane.dispatchEvent(new Event('scroll', { bubbles: true }));
                  return pane.scrollTop !== before || before === 0;
                }"""
            )
        )
    except Exception:
        return False


def _click_ig_inbox_thread(page: Any, title: str) -> bool:
    try:
        return bool(
            page.evaluate(
                """(targetTitle) => {
                  const panes = Array.from(document.querySelectorAll('div')).filter(el => {
                    const oy = getComputedStyle(el).overflowY;
                    const r = el.getBoundingClientRect();
                    return (oy === 'auto' || oy === 'scroll') && r.left < 300 && r.width > 180 && r.width < 520 && el.clientHeight > 300;
                  });
                  const inbox = panes[0] || document.body;
                  for (const el of inbox.querySelectorAll('[role="listitem"], [role="button"]')) {
                    const raw = (el.innerText || '').trim();
                    if (!raw) continue;
                    const first = raw.split('\\n')[0].trim();
                    if (first === targetTitle) {
                      el.click();
                      return true;
                    }
                  }
                  return false;
                }""",
                title,
            )
        )
    except Exception:
        return False


def _sync_ig_dm(
    page: Any,
    request_context: Any,
    baseline: ArchiveBaseline,
    config: SyncConfig,
    delta: SyncDelta,
) -> None:
    """Incrementally collect 2026 IG DM messages via live DOM + attempt missing DM media recovery."""
    cutoff_ms = baseline.latest_message_ms("instagram")
    missing_targets = baseline.missing_dm_media("instagram") if config.repair_ig_dm_media else []
    viewer_name = (
        (baseline.ig_profile.full_name or baseline.ig_profile.username)
        if baseline.ig_profile
        else "Instagram User"
    ) or "Instagram User"

    page.goto("https://www.instagram.com/direct/inbox/", wait_until="domcontentloaded")
    time.sleep(3.5)
    check_page_safety(str(getattr(page, "url", "")), "")

    # 1. Quickly try direct download on any missing DM image URIs first
    still_missing: list[MissingDmMediaTarget] = []
    repair_dir = config.staging_dir / "media" / "instagram" / "dm_repairs"
    os.makedirs(to_long_path(repair_dir), exist_ok=True)
    for tgt in missing_targets:
        stem = hashlib.sha1(f"{tgt.thread_id}:{tgt.message_index}:{tgt.media_index}".encode()).hexdigest()[:16]
        saved = _download_staged_asset(
            request_context,
            tgt.original_uri,
            repair_dir / stem,
            referer="https://www.instagram.com/",
            media_type="image",
        )
        if saved is not None:
            chk = verify_image(saved)
            delta.dm_media_repairs.append(
                DmMediaRepair(
                    thread_id=tgt.thread_id,
                    message_index=tgt.message_index,
                    media_index=tgt.media_index,
                    staged_file=saved,
                    width=chk.width,
                    height=chk.height,
                )
            )
        else:
            still_missing.append(tgt)

    # 2. Read live 2026 inbox list from left pane
    try:
        inbox_rows = page.evaluate(_IG_DM_INBOX_LIST_JS)
        if not isinstance(inbox_rows, list):
            inbox_rows = []
    except Exception:
        inbox_rows = []

    if not inbox_rows:
        delta.notes.append("Could not read Instagram Direct inbox list from DOM")
        return

    msg_media_dir = config.staging_dir / "media" / "instagram" / "messages"
    os.makedirs(to_long_path(msg_media_dir), exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    # Give a 2-day slack on coarse relative labels like "5일" or "1주"
    inbox_slack_ms = 2 * 86400 * 1000

    if config.sync_ig_dm:
        for row in inbox_rows:
            raw_title = str(row.get("title") or "").strip()
            time_label = str(row.get("timeLabel") or "").strip()
            if not raw_title or not time_label:
                continue
            rel_dt = parse_fb_messenger_timestamp(time_label, now=now_utc)
            if rel_dt is not None and int(rel_dt.timestamp() * 1000) < (cutoff_ms - inbox_slack_ms):
                continue

            if not _click_ig_inbox_thread(page, raw_title):
                continue
            time.sleep(2.5)
            check_page_safety(str(getattr(page, "url", "")), "")

            cur_url = str(getattr(page, "url", ""))
            m_tid = re.search(r"/direct/t/([^/?#]+)", cur_url)
            tid = m_tid.group(1) if m_tid else hashlib.sha1(raw_title.encode("utf-8")).hexdigest()[:16]

            # Scroll up slightly if the oldest visible date header is still newer than cutoff_ms
            for _ in range(8):
                pane_data = page.evaluate(_IG_DM_PANE_LEAVES_JS)
                if not isinstance(pane_data, dict) or not pane_data.get("found"):
                    break
                header_dts = [
                    dt
                    for lf in pane_data.get("leaves") or []
                    if lf.get("tag") == "SPAN"
                    and int(lf.get("leftPct") or 0) <= 5
                    and int(lf.get("rightPct") or 0) >= 90
                    and (dt := parse_fb_messenger_timestamp(str(lf.get("text") or ""), now=now_utc)) is not None
                ]
                if header_dts and int(min(header_dts).timestamp() * 1000) <= cutoff_ms:
                    break
                if not _scroll_ig_pane_up(page):
                    break
                time.sleep(1.5)

            pane_data = page.evaluate(_IG_DM_PANE_LEAVES_JS)
            leaves = pane_data.get("leaves") if isinstance(pane_data, dict) else []
            if not leaves:
                continue

            is_group = ("," in raw_title) or (" 외 " in raw_title) or (not raw_title.endswith("님"))
            parsed = parse_ig_direct_dom_leaves(
                leaves,
                viewer_name=viewer_name,
                thread_title=raw_title,
                is_group=is_group,
                cutoff_ms=cutoff_ms,
                now=now_utc,
            )
            if not parsed:
                continue

            new_messages: list[Message] = []
            for msg, downloads in parsed:
                for med_idx, m_url, m_type in downloads:
                    stem = hashlib.sha1(f"{tid}:{msg.timestamp_ms}:{med_idx}:{m_url}".encode()).hexdigest()[:16]
                    saved = _download_staged_asset(
                        request_context,
                        m_url,
                        msg_media_dir / f"ig_sync_{stem}",
                        referer="https://www.instagram.com/",
                        media_type=m_type,
                    )
                    if saved is not None:
                        msg.media[med_idx].local_path = saved.relative_to(config.staging_dir).as_posix()
                new_messages.append(msg)

            if new_messages:
                delta.message_threads.append(
                    MessageThread(
                        platform="instagram",
                        thread_id=tid,
                        title=_clean_ig_dm_title(raw_title),
                        participants=[UserRef(full_name=viewer_name), UserRef(full_name=_clean_ig_dm_title(raw_title))],
                        messages=new_messages,
                    )
                )

    # 3. Best-effort DOM recovery for expired IG DM images via asset ID matching
    if still_missing:
        missing_by_clean: dict[str, list[MissingDmMediaTarget]] = {}
        for tgt in still_missing:
            key = _clean_ig_dm_title(tgt.thread_title or "").lower()
            if key and key not in ("instagram user", "instagram 사용자"):
                missing_by_clean.setdefault(key, []).append(tgt)
            else:
                delta.notes.append(
                    f"Expired IG DM image in deleted/anonymous thread '{tgt.thread_title}' cannot be recovered from live web"
                )

        inbox_by_clean = {_clean_ig_dm_title(str(r.get("title") or "")).lower(): str(r.get("title") or "") for r in inbox_rows}
        for clean_key, targets in missing_by_clean.items():
            raw_title = inbox_by_clean.get(clean_key)
            if not raw_title:
                delta.notes.append(
                    f"IG DM thread '{targets[0].thread_title}' not visible in top inbox list for expired-image recovery ({len(targets)} items)"
                )
                continue
            target_asset_ids: dict[str, MissingDmMediaTarget] = {}
            for t in targets:
                m_aid = _IG_ASSET_ID_RE.search(t.original_uri)
                if m_aid:
                    target_asset_ids[m_aid.group(1)] = t
            if not target_asset_ids:
                continue
            if not _click_ig_inbox_thread(page, raw_title):
                continue
            time.sleep(2.5)
            found_urls: dict[str, str] = {}
            for _ in range(15):
                pane_data = page.evaluate(_IG_DM_PANE_LEAVES_JS)
                for lf in (pane_data.get("leaves") if isinstance(pane_data, dict) else []) or []:
                    if lf.get("tag") == "IMG" and lf.get("src"):
                        s_url = str(lf["src"])
                        for aid in target_asset_ids:
                            if aid in s_url and aid not in found_urls:
                                found_urls[aid] = s_url
                if len(found_urls) == len(target_asset_ids):
                    break
                if not _scroll_ig_pane_up(page):
                    break
                time.sleep(1.2)
            for aid, tgt in target_asset_ids.items():
                fresh_url = found_urls.get(aid)
                if not fresh_url:
                    continue
                stem = hashlib.sha1(f"{tgt.thread_id}:{tgt.message_index}:{tgt.media_index}".encode()).hexdigest()[:16]
                saved = _download_staged_asset(
                    request_context,
                    fresh_url,
                    repair_dir / stem,
                    referer="https://www.instagram.com/",
                    media_type="image",
                )
                if saved is not None:
                    chk = verify_image(saved)
                    delta.dm_media_repairs.append(
                        DmMediaRepair(
                            thread_id=tgt.thread_id,
                            message_index=tgt.message_index,
                            media_index=tgt.media_index,
                            staged_file=saved,
                            width=chk.width,
                            height=chk.height,
                        )
                    )
            unrecovered = len(target_asset_ids) - len(found_urls)
            if unrecovered > 0:
                delta.notes.append(
                    f"IG DM thread '{targets[0].thread_title}': {unrecovered}/{len(target_asset_ids)} old 2020-2022 images not found in recent scroll window (likely expired/ephemeral DM media)"
                )


def _repair_ig_silent_videos(
    page: Any,
    request_context: Any,
    baseline: ArchiveBaseline,
    config: SyncConfig,
    delta: SyncDelta,
) -> None:
    """Visit the silent IG post shortcodes, inspect live audio flags/DASH streams, and mux audio if present."""
    targets = baseline.silent_post_videos("instagram")
    if not targets:
        return

    by_shortcode: dict[str, list[SilentVideoTarget]] = {}
    for tgt in targets:
        by_shortcode.setdefault(tgt.shortcode, []).append(tgt)

    collector = ResponseCollector(url_substrings=("/graphql/query", "/api/graphql", "/api/v1/"))
    collector.attach(page)
    stage_dir = config.staging_dir / "media" / "instagram" / "video_repairs"
    os.makedirs(to_long_path(stage_dir), exist_ok=True)

    try:
        for sc, sc_targets in by_shortcode.items():
            collector.drain()
            url = f"https://www.instagram.com/p/{sc}/"
            page.goto(url, wait_until="domcontentloaded")
            config.cooldown.wait_step()
            collector.raise_if_unsafe()
            check_page_safety(str(getattr(page, "url", url)), "")

            payloads: list[dict[str, Any]] = []
            try:
                html = page.content()
                if isinstance(html, str) and html:
                    payloads.extend(extract_ssr_json_payloads(html))
            except Exception:
                pass
            for resp in collector.drain():
                payloads.extend(resp.payloads)

            # Inspect explicit `has_audio` flags per media node
            raw_audio_flags: list[bool | None] = []
            for p in payloads:
                for node in _walk_dicts(p):
                    if node.get("code") == sc or node.get("shortcode") == sc:
                        cm = node.get("carousel_media")
                        if isinstance(cm, list) and cm:
                            raw_audio_flags = [
                                (item.get("has_audio") if isinstance(item.get("has_audio"), bool) else None)
                                for item in cm
                                if isinstance(item, dict)
                            ]
                            break
                        if isinstance(node.get("has_audio"), bool):
                            raw_audio_flags = [node["has_audio"]]
                            break
                if raw_audio_flags:
                    break

            _, candidates = extract_ig_post_from_payloads(sc, payloads)
            non_cover_cands = [c for c in candidates if not c.is_carousel_cover] or candidates

            for tgt in sc_targets:
                cand = (
                    non_cover_cands[tgt.media_index]
                    if tgt.media_index < len(non_cover_cands)
                    else next((c for c in non_cover_cands if c.media_type == "video"), None)
                )
                explicit_has_audio = (
                    raw_audio_flags[tgt.media_index]
                    if tgt.media_index < len(raw_audio_flags)
                    else (raw_audio_flags[0] if len(raw_audio_flags) == 1 else None)
                )

                if cand is None or cand.media_type != "video":
                    delta.video_audio_repairs.append(
                        VideoAudioRepair(
                            post_id=tgt.post_id,
                            shortcode=sc,
                            media_index=tgt.media_index,
                            status="unavailable",
                            detail="Video candidate not found in live IG payload",
                        )
                    )
                    continue

                out_mp4 = stage_dir / f"{sc}_{tgt.media_index:02d}_muxed.mp4"
                repaired = False
                if cand.dash_audio_url:
                    tmp_v = stage_dir / f".{sc}_{tgt.media_index:02d}_v.mp4"
                    tmp_a = stage_dir / f".{sc}_{tgt.media_index:02d}_a.m4a"
                    try:
                        v_src = cand.dash_video_url or cand.progressive_url
                        if v_src:
                            download_media_via_context(request_context, v_src, tmp_v, headers={"Referer": "https://www.instagram.com/"})
                            video_in = tmp_v
                        else:
                            video_in = baseline.archive_dir.joinpath(*tgt.local_path.split("/"))
                        download_media_via_context(request_context, cand.dash_audio_url, tmp_a, headers={"Referer": "https://www.instagram.com/"})
                        mux_dash_streams(video_in, tmp_a, out_mp4)
                        if inspect_mp4(out_mp4).has_audio:
                            repaired = True
                    except Exception as exc:
                        delta.notes.append(f"DASH mux retry failed for {sc}#{tgt.media_index}: {exc}")
                    finally:
                        for tp in (tmp_v, tmp_a):
                            if tp.exists():
                                try:
                                    os.remove(to_long_path(tp))
                                except OSError:
                                    pass
                elif cand.progressive_url:
                    try:
                        download_media_via_context(request_context, cand.progressive_url, out_mp4, headers={"Referer": "https://www.instagram.com/"})
                        if inspect_mp4(out_mp4).has_audio:
                            repaired = True
                        else:
                            os.remove(to_long_path(out_mp4))
                    except Exception:
                        pass

                if repaired:
                    delta.video_audio_repairs.append(
                        VideoAudioRepair(
                            post_id=tgt.post_id,
                            shortcode=sc,
                            media_index=tgt.media_index,
                            status="repaired",
                            staged_file=out_mp4,
                            detail="Audio track recovered and muxed from Instagram web",
                        )
                    )
                else:
                    reason = (
                        "Instagram metadata explicitly reports has_audio=false (uploaded muted)"
                        if explicit_has_audio is False
                        else "No audio track present in Instagram progressive or DASH streams"
                    )
                    delta.video_audio_repairs.append(
                        VideoAudioRepair(
                            post_id=tgt.post_id,
                            shortcode=sc,
                            media_index=tgt.media_index,
                            status="confirmed_silent",
                            detail=reason,
                        )
                    )
    finally:
        collector.detach()


def _sync_ig_profile_delta(
    page: Any,
    request_context: Any,
    baseline: ArchiveBaseline,
    config: SyncConfig,
    delta: SyncDelta,
) -> None:
    """Check Instagram profile for new posts, new comments on recent posts, active stories, and connection deltas."""
    ig_prof = baseline.ig_profile
    if ig_prof is None or not ig_prof.username:
        return
    username = ig_prof.username
    known_shortcodes = baseline.known_shortcodes("instagram")
    known_story_ids = baseline.known_story_ids("instagram")

    collector = ResponseCollector(url_substrings=("/graphql/query", "/api/graphql", "/api/v1/"))
    collector.attach(page)
    try:
        collector.drain()
        prof_url = f"https://www.instagram.com/{username}/"
        page.goto(prof_url, wait_until="domcontentloaded")
        config.cooldown.wait_step()
        collector.raise_if_unsafe()
        check_page_safety(str(getattr(page, "url", prof_url)), "")

        payloads: list[dict[str, Any]] = []
        try:
            html = page.content()
            if isinstance(html, str) and html:
                payloads.extend(extract_ssr_json_payloads(html))
        except Exception:
            pass
        for r in collector.drain():
            payloads.extend(r.payloads)

        live_prof, _ = extract_ig_profile_from_payloads(payloads, target_username=username)
        if live_prof is not None:
            delta.profiles.append(live_prof)

        dom_codes: list[str] = []
        try:
            hrefs = page.evaluate("() => Array.from(document.querySelectorAll('main a[href]'), a => a.getAttribute('href') || '')")
            if isinstance(hrefs, list):
                dom_codes = extract_ig_dom_shortcodes(hrefs)
        except Exception:
            pass

        top_codes = list(
            dict.fromkeys(
                [
                    *dom_codes,
                    *extract_ig_shortcodes_from_payloads(payloads, owner_username=username),
                ]
            )
        )
        new_codes = [sc for sc in top_codes if sc not in known_shortcodes]

        # Check recent existing posts for increased comment/like counts in grid payloads
        existing_by_sc = {p.shortcode: p for p in baseline.posts if p.platform == "instagram" and p.shortcode}
        updated_codes: list[str] = []
        for p_dict in payloads:
            for node in _walk_dicts(p_dict):
                sc = node.get("code") or node.get("shortcode")
                if not isinstance(sc, str) or sc not in existing_by_sc:
                    continue
                ex = existing_by_sc[sc]
                live_cmts = node.get("comment_count")
                if live_cmts is None and isinstance(node.get("edge_media_to_comment"), dict):
                    live_cmts = node["edge_media_to_comment"].get("count")
                live_likes = node.get("like_count")
                if live_likes is None and isinstance(node.get("edge_liked_by"), dict):
                    live_likes = node["edge_liked_by"].get("count")
                ex_cmt_cnt = sum(1 + len(c.replies) for c in ex.comments)
                if (isinstance(live_cmts, int) and live_cmts > ex_cmt_cnt) or (
                    isinstance(live_likes, int) and (ex.likes.count is None or live_likes > ex.likes.count)
                ):
                    if sc not in updated_codes and sc not in new_codes:
                        updated_codes.append(sc)

        target_codes = [*new_codes, *updated_codes[: config.recent_posts_check_limit]]
        for sc in target_codes:
            collector.drain()
            p_url = f"https://www.instagram.com/p/{sc}/"
            page.goto(p_url, wait_until="domcontentloaded")
            config.cooldown.wait_step()
            collector.raise_if_unsafe()
            p_payloads: list[dict[str, Any]] = []
            try:
                p_html = page.content()
                if isinstance(p_html, str) and p_html:
                    p_payloads.extend(extract_ssr_json_payloads(p_html))
            except Exception:
                pass
            for r in collector.drain():
                p_payloads.extend(r.payloads)
            post, cands = extract_ig_post_from_payloads(sc, p_payloads)
            if post is None:
                continue
            if sc in new_codes and cands:
                try:
                    post.media = download_post_media(
                        request_context,
                        sc,
                        cands,
                        config.staging_dir / "media" / "instagram" / "posts",
                        created_timestamp=post.timestamp_unix,
                    )
                except Exception as exc:
                    delta.notes.append(f"Media download warning for new IG post {sc}: {exc}")
            delta.posts.append(post)

        # Active stories check via web API
        uid = (live_prof.user_id if live_prof else None) or ig_prof.user_id
        hdrs = _ig_api_headers(page)
        if uid:
            st_status, st_data = _page_fetch_json(
                page,
                f"https://www.instagram.com/api/v1/feed/reels_media/?reel_ids={urllib.parse.quote(uid)}",
                headers=hdrs,
            )
            if st_status == 200 and isinstance(st_data, dict):
                reels = st_data.get("reels_media") or []
                story_dir = config.staging_dir / "media" / "instagram" / "stories"
                os.makedirs(to_long_path(story_dir), exist_ok=True)
                for reel in reels:
                    if not isinstance(reel, dict):
                        continue
                    for item in reel.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        sid = str(item.get("id") or item.get("pk") or "")
                        if not sid or sid in known_story_ids:
                            continue
                        ts = int(item.get("taken_at") or 0) or None
                        cand = _media_candidate_from_node(item)
                        m_url = cand.progressive_url or cand.dash_video_url
                        saved_media: MediaItem | None = None
                        if m_url:
                            saved = _download_staged_asset(
                                request_context,
                                m_url,
                                story_dir / f"sync_story_{sid}",
                                referer="https://www.instagram.com/",
                                media_type=cand.media_type,
                            )
                            if saved is not None:
                                saved_media = MediaItem(
                                    media_id=cand.media_id,
                                    media_type=cand.media_type,
                                    local_path=saved.relative_to(config.staging_dir).as_posix(),
                                    original_uri=m_url,
                                    width=cand.width,
                                    height=cand.height,
                                )
                        delta.stories.append(
                            Story(
                                platform="instagram",
                                story_id=sid,
                                created_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                                timestamp_unix=ts,
                                source="highlight_web",
                                media=saved_media,
                            )
                        )

            # Followers / following delta via lightweight friendship API
            for rel, ep in (("follower", "followers"), ("following", "following")):
                max_id: str | None = None
                for _ in range(10):
                    f_url = f"https://www.instagram.com/api/v1/friendships/{urllib.parse.quote(uid)}/{ep}/?count=50"
                    if max_id:
                        f_url += f"&max_id={urllib.parse.quote(max_id)}"
                    fst, fdata = _page_fetch_json(page, f_url, headers=hdrs)
                    if fst != 200 or not isinstance(fdata, dict):
                        break
                    for u in fdata.get("users") or []:
                        if not isinstance(u, dict) or not u.get("username"):
                            continue
                        uname = str(u["username"])
                        delta.connections.append(
                            Connection(
                                platform="instagram",
                                relation=rel,  # type: ignore[arg-type]
                                user_id=str(u.get("pk") or u.get("id") or "") or None,
                                username=uname,
                                full_name=u.get("full_name") if isinstance(u.get("full_name"), str) else None,
                                profile_url=f"https://www.instagram.com/{uname}/",
                                match_confidence="exact",
                            )
                        )
                    next_id = fdata.get("next_max_id")
                    if not next_id:
                        break
                    max_id = str(next_id)
                    config.cooldown.wait_step()
    finally:
        collector.detach()


def _detect_fb_pin_prompt(page: Any) -> bool:
    try:
        text = page.inner_text("body") or ""
    except Exception:
        return False
    low = text.lower()
    markers = (
        "pin을 입력",
        "pin 입력",
        "채팅 기록 복원",
        "보안 저장소",
        "enter your pin",
        "restore chat history",
        "one-time code",
    )
    return any(m in low for m in markers)


_FB_SIDEBAR_THREADS_JS = """() => {
  const links = Array.from(document.querySelectorAll('a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]'));
  const seen = new Set();
  const out = [];
  for (const a of links) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\\/messages\\/(e2ee\\/)?t\\/([^/?#]+)/);
    if (!m) continue;
    const isE2ee = Boolean(m[1]);
    const tid = m[2];
    const key = (isE2ee ? 'e2ee:' : '') + tid;
    if (seen.has(key)) continue;
    seen.add(key);
    const lines = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    out.push({
      href,
      threadId: tid,
      isE2ee,
      title: lines[0] || tid,
      lines,
    });
  }
  return out;
}"""

_FB_THREAD_MESSAGES_DOM_JS = """() => {
  const main = document.querySelector('[role="main"]') || document.body;
  const rows = Array.from(main.querySelectorAll('[role="row"]'));
  const items = [];
  for (const row of rows) {
    const rawText = (row.innerText || '').trim();
    const rect = row.getBoundingClientRect();
    const mainRect = main.getBoundingClientRect();
    const centerRatio = mainRect.width > 0 ? ((rect.left + rect.right) / 2 - mainRect.left) / mainRect.width : 0.5;
    const imgs = Array.from(row.querySelectorAll('img[src]'))
      .filter(img => {
        const w = img.naturalWidth || img.width || 0;
        const h = img.naturalHeight || img.height || 0;
        return w >= 48 && h >= 48 && !/emoji|static\\.xx\\.fbcdn/.test(img.src);
      })
      .map(img => img.src);
    const vids = Array.from(row.querySelectorAll('video[src], video source[src]')).map(v => v.src).filter(Boolean);
    const links = Array.from(row.querySelectorAll('a[href^="http"]')).map(a => a.href).filter(Boolean);
    const dirDivs = Array.from(row.querySelectorAll('[dir="auto"]')).map(d => (d.innerText || '').trim()).filter(Boolean);
    items.push({
      rawText,
      dirTexts: dirDivs,
      isRightAligned: centerRatio > 0.55,
      imgs,
      vids,
      links,
    });
  }
  return items;
}"""


def parse_fb_messenger_dom_rows(
    rows: Sequence[dict[str, Any]],
    *,
    owner_name: str,
    other_name: str,
    cutoff_ms: int = 0,
    now: datetime | None = None,
) -> list[tuple[Message, list[tuple[int, str, str]]]]:
    """Parse extracted Messenger `[role="row"]` DOM snapshots into canonical `Message` objects."""
    current_dt: datetime | None = None
    out: list[tuple[Message, list[tuple[int, str, str]]]] = []

    for row in rows:
        raw = str(row.get("rawText") or "").strip()
        dir_texts = [str(t).strip() for t in (row.get("dirTexts") or []) if str(t).strip()]
        imgs = [str(u) for u in (row.get("imgs") or []) if isinstance(u, str) and u]
        vids = [str(u) for u in (row.get("vids") or []) if isinstance(u, str) and u]
        links = [str(u) for u in (row.get("links") or []) if isinstance(u, str) and u]

        # Pure timestamp / date separator row
        if not imgs and not vids and raw and len(raw) <= 45:
            parsed_dt = parse_fb_messenger_timestamp(raw, now=now)
            if parsed_dt is not None:
                current_dt = parsed_dt
                continue

        # Sometimes a date header appears as the first line inside a row
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            head_dt = parse_fb_messenger_timestamp(lines[0], now=now)
            if head_dt is not None and len(lines) > 1:
                current_dt = head_dt
                lines = lines[1:]

        is_right = bool(row.get("isRightAligned"))
        sender = owner_name if is_right else other_name
        # Filter out sender label line if repeated at top
        body_lines = [
            ln
            for ln in (dir_texts or lines)
            if ln not in ("전송함", "읽음", "Sent", "Seen", "Delivered", "보냄", sender, owner_name)
        ]
        # Remove pure timestamp lines from body
        cleaned_lines: list[str] = []
        for ln in body_lines:
            pdt = parse_fb_messenger_timestamp(ln, now=now)
            if pdt is not None and len(ln) <= 35:
                current_dt = pdt
            else:
                cleaned_lines.append(ln)

        text = "\n".join(dict.fromkeys(cleaned_lines)).strip() or None
        if not text and not imgs and not vids and not links:
            continue

        ts_ms = int(current_dt.timestamp() * 1000) if current_dt is not None else 0
        if cutoff_ms and ts_ms <= cutoff_ms:
            continue

        media_items: list[MediaItem] = []
        downloads: list[tuple[int, str, str]] = []
        for u in imgs:
            idx = len(media_items)
            media_items.append(MediaItem(media_type="image", original_uri=u))
            downloads.append((idx, u, "image"))
        for u in vids:
            idx = len(media_items)
            media_items.append(MediaItem(media_type="video", original_uri=u))
            downloads.append((idx, u, "video"))

        msg = Message(
            sender=sender,
            timestamp_unix=(ts_ms // 1000) if ts_ms else None,
            timestamp_ms=ts_ms or None,
            created_at=current_dt,
            text=text,
            media=media_items,
            shared_link=links[0] if links else None,
        )
        out.append((msg, downloads))

    return out


def _sync_fb_delta(
    page: Any,
    request_context: Any,
    baseline: ArchiveBaseline,
    config: SyncConfig,
    delta: SyncDelta,
) -> None:
    """Inspect FB Messenger (including E2EE threads after cutoff) and FB timeline/friends delta."""
    fb_cutoff_ms = baseline.latest_message_ms("facebook")
    fb_owner = (
        (baseline.fb_profile.full_name or baseline.fb_profile.username)
        if baseline.fb_profile
        else "Facebook User"
    ) or "Facebook User"

    if config.sync_fb_messenger:
        page.goto("https://www.facebook.com/messages/t/", wait_until="domcontentloaded")
        config.cooldown.wait_step()
        check_page_safety(str(getattr(page, "url", "")), "")

        if _detect_fb_pin_prompt(page) and config.wait_pin_sec > 0:
            delta.notes.append("FB Messenger E2EE PIN prompt detected; waited for manual unlock")
            deadline = time.monotonic() + config.wait_pin_sec
            while time.monotonic() < deadline and _detect_fb_pin_prompt(page):
                time.sleep(2.0)

        sidebar_threads: list[dict[str, Any]] = []
        try:
            raw_sidebar = page.evaluate(_FB_SIDEBAR_THREADS_JS)
            if isinstance(raw_sidebar, list):
                sidebar_threads = raw_sidebar
        except Exception:
            sidebar_threads = []

        msg_media_dir = config.staging_dir / "media" / "facebook" / "messages"
        os.makedirs(to_long_path(msg_media_dir), exist_ok=True)

        inspected = 0
        for item in sidebar_threads:
            if inspected >= config.max_fb_threads:
                break
            lines = item.get("lines") or []
            # Check if sidebar shows a timestamp clearly older than cutoff
            sidebar_dt: datetime | None = None
            for ln in reversed(lines):
                cand_dt = parse_fb_messenger_timestamp(str(ln))
                if cand_dt is not None:
                    sidebar_dt = cand_dt
                    break
            is_e2ee = bool(item.get("isE2ee"))
            if not is_e2ee and sidebar_dt is not None and int(sidebar_dt.timestamp() * 1000) <= fb_cutoff_ms:
                continue

            inspected += 1
            href = str(item.get("href") or "")
            full_url = urllib.parse.urljoin("https://www.facebook.com/", href)
            page.goto(full_url, wait_until="domcontentloaded")
            config.cooldown.wait_step()
            check_page_safety(str(getattr(page, "url", full_url)), "")

            # Scroll message pane up slightly to load recent history past cutoff
            for _ in range(5):
                try:
                    scrolled = page.evaluate(
                        """() => {
                            const main = document.querySelector('[role="main"]');
                            if (!main) return false;
                            for (const el of main.querySelectorAll('div')) {
                                if (el.scrollHeight > el.clientHeight + 80 && el.scrollTop > 0) {
                                    el.scrollTop = Math.max(0, el.scrollTop - el.clientHeight);
                                    return true;
                                }
                            }
                            return false;
                        }"""
                    )
                    if not scrolled:
                        break
                    config.cooldown.wait_step()
                except Exception:
                    break

            try:
                dom_rows = page.evaluate(_FB_THREAD_MESSAGES_DOM_JS)
            except Exception:
                dom_rows = []
            if not isinstance(dom_rows, list) or not dom_rows:
                continue

            title = str(item.get("title") or item.get("threadId") or "")
            parsed_msgs = parse_fb_messenger_dom_rows(
                dom_rows,
                owner_name=fb_owner,
                other_name=title,
                cutoff_ms=fb_cutoff_ms,
            )
            if not parsed_msgs:
                continue

            final_msgs: list[Message] = []
            for msg, downloads in parsed_msgs:
                for med_idx, m_url, m_type in downloads:
                    stem = hashlib.sha1(f"{item['threadId']}:{msg.timestamp_ms}:{med_idx}:{m_url}".encode()).hexdigest()[:16]
                    saved = _download_staged_asset(
                        request_context,
                        m_url,
                        msg_media_dir / f"fb_sync_{stem}",
                        referer="https://www.facebook.com/",
                        media_type=m_type,
                    )
                    if saved is not None:
                        msg.media[med_idx].local_path = saved.relative_to(config.staging_dir).as_posix()
                final_msgs.append(msg)

            if final_msgs:
                tid = f"e2ee/{item['threadId']}" if is_e2ee else f"inbox/{item['threadId']}"
                delta.message_threads.append(
                    MessageThread(
                        platform="facebook",
                        thread_id=tid,
                        title=title,
                        participants=[UserRef(full_name=fb_owner), UserRef(full_name=title)],
                        is_e2ee=is_e2ee,
                        messages=final_msgs,
                    )
                )

    if config.sync_fb_timeline:
        known_fb_ids = baseline.known_post_ids("facebook")
        fb_latest_post = baseline.latest_post_unix("facebook")
        collector = ResponseCollector(url_substrings=("/api/graphql", "/graphql"))
        collector.attach(page)
        try:
            collector.drain()
            page.goto("https://www.facebook.com/me", wait_until="domcontentloaded")
            config.cooldown.wait_step()
            collector.raise_if_unsafe()
            payloads: list[dict[str, Any]] = []
            try:
                html = page.content()
                if isinstance(html, str) and html:
                    payloads.extend(extract_ssr_json_payloads(html))
            except Exception:
                pass
            for _ in range(3):
                for r in collector.drain():
                    payloads.extend(r.payloads)
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                config.cooldown.wait_step()
            for r in collector.drain():
                payloads.extend(r.payloads)

            fb_posts, fb_media_cands = extract_fb_posts_from_payloads(payloads)
            fb_post_media_dir = config.staging_dir / "media" / "facebook" / "posts"
            os.makedirs(to_long_path(fb_post_media_dir), exist_ok=True)
            for post in fb_posts:
                pid = post.post_id
                if pid not in known_fb_ids and (post.timestamp_unix or 0) > fb_latest_post:
                    cands_for_post = [c for c in fb_media_cands if c.post_id == pid and not c.is_link_thumbnail]
                    for idx, cand in enumerate(cands_for_post):
                        m_url = cand.progressive_url or cand.dash_video_url
                        if not m_url:
                            continue
                        saved = _download_staged_asset(
                            request_context,
                            m_url,
                            fb_post_media_dir / f"fb_sync_{pid}_{idx:02d}",
                            referer="https://www.facebook.com/",
                            media_type=cand.media_type,
                        )
                        if saved is not None:
                            post.media.append(
                                MediaItem(
                                    media_id=cand.media_id,
                                    media_type=cand.media_type,
                                    local_path=saved.relative_to(config.staging_dir).as_posix(),
                                    original_uri=m_url,
                                    width=cand.width,
                                    height=cand.height,
                                )
                            )
                    delta.posts.append(post)

            # Quick friends delta check
            collector.drain()
            page.goto("https://www.facebook.com/me/friends", wait_until="domcontentloaded")
            config.cooldown.wait_step()
            f_payloads: list[dict[str, Any]] = []
            try:
                f_html = page.content()
                if isinstance(f_html, str) and f_html:
                    f_payloads.extend(extract_ssr_json_payloads(f_html))
            except Exception:
                pass
            for _ in range(2):
                for r in collector.drain():
                    f_payloads.extend(r.payloads)
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                config.cooldown.wait_step()
            for r in collector.drain():
                f_payloads.extend(r.payloads)
            fb_friends, _avatar_urls = extract_fb_friends_from_payloads(f_payloads)
            delta.connections.extend(fb_friends)
        finally:
            collector.detach()


def collect_incremental_sync(config: SyncConfig) -> SyncDelta:
    """Attach to a logged-in Chrome CDP session and collect all incremental IG/FB deltas & repairs."""
    from playwright.sync_api import sync_playwright

    baseline = load_archive_baseline(config.archive_dir)
    os.makedirs(to_long_path(config.staging_dir), exist_ok=True)
    delta = SyncDelta(staged_roots=[config.staging_dir])

    with sync_playwright() as pw:
        browser, context = connect_cdp(pw, config.cdp_url)
        page = context.new_page()
        request_context = context.request
        try:
            if config.sync_ig:
                if config.sync_ig_dm or config.repair_ig_dm_media:
                    _sync_ig_dm(page, request_context, baseline, config, delta)
                if config.repair_ig_silent_videos:
                    _repair_ig_silent_videos(page, request_context, baseline, config, delta)
                if config.sync_ig_profile:
                    _sync_ig_profile_delta(page, request_context, baseline, config, delta)
            if config.sync_fb:
                _sync_fb_delta(page, request_context, baseline, config, delta)
        except SafetyStopError as exc:
            delta.notes.append(f"Paused safely due to Meta guard: {exc}")
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    return delta
