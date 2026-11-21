"""Tests for Chrome CDP launcher, response interceptor, SSR/NDJSON parsing, and safety guards."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from meta_archive_pipeline.collect.cdp import (
    CooldownTracker,
    ResponseCollector,
    SafetyStopError,
    build_chrome_launch_args,
    check_page_safety,
    check_response_safety,
    extract_ssr_json_payloads,
    parse_ndjson_payload,
)


def test_build_chrome_launch_args(tmp_path: Path) -> None:
    chrome_exe = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    profile_dir = tmp_path / "chrome-profile"

    args = build_chrome_launch_args(
        chrome_exe,
        profile_dir,
        port=9222,
        host="127.0.0.1",
        start_url="https://www.instagram.com/",
    )

    assert args[0] == str(chrome_exe)
    assert f"--user-data-dir={os.path.abspath(os.fspath(profile_dir))}" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=9222" in args
    assert args[-1] == "https://www.instagram.com/"


def test_safety_checks_trigger_on_429_login_checkpoint_and_automated() -> None:
    check_response_safety("https://www.instagram.com/graphql/query", 200, '{"ok": true}')
    check_page_safety("https://www.instagram.com/yungis_/", "Normal profile page")

    with pytest.raises(SafetyStopError) as exc_429:
        check_response_safety("https://www.instagram.com/api/v1/feed/", 429, "")
    assert exc_429.value.reason == "rate_limited"
    assert exc_429.value.status == 429

    with pytest.raises(SafetyStopError) as exc_login:
        check_response_safety("https://www.instagram.com/accounts/login/?next=%2Fp%2Fabc", 200, "")
    assert exc_login.value.reason == "login_redirect"

    with pytest.raises(SafetyStopError) as exc_fb_login:
        check_page_safety("https://www.facebook.com/login.php?next=...", "")
    assert exc_fb_login.value.reason == "login_redirect"

    with pytest.raises(SafetyStopError) as exc_cp_url:
        check_page_safety("https://www.instagram.com/challenge/?next=/", "")
    assert exc_cp_url.value.reason == "checkpoint"

    with pytest.raises(SafetyStopError) as exc_cp_body:
        check_response_safety("https://www.instagram.com/api/v1/a", 400, '{"message":"checkpoint_required"}')
    assert exc_cp_body.value.reason == "checkpoint"

    with pytest.raises(SafetyStopError) as exc_auto_en:
        check_page_safety("https://www.instagram.com/yungis_/", "We suspect automated behavior on your account")
    assert exc_auto_en.value.reason == "automated_behavior"

    with pytest.raises(SafetyStopError) as exc_auto_ko:
        check_page_safety("https://www.instagram.com/yungis_/", "자동화된 행동이 감지되었습니다. 잠시 후 다시 시도하세요.")
    assert exc_auto_ko.value.reason == "automated_behavior"


def test_parse_ndjson_payload_variants() -> None:
    assert parse_ndjson_payload("") == []
    assert parse_ndjson_payload('for (;;);{"data": {"id": 1}}') == [{"data": {"id": 1}}]
    assert parse_ndjson_payload('[{"a": 1}, "ignored", {"b": 2}]') == [{"a": 1}, {"b": 2}]

    ndjson = (
        'for (;;);{"chunk": 1}\n'
        "\n"
        "not-json-noise\n"
        '{"chunk": 2}\n'
        '{"chunk": 3}'
    )
    assert parse_ndjson_payload(ndjson) == [{"chunk": 1}, {"chunk": 2}, {"chunk": 3}]


def test_extract_ssr_json_payloads() -> None:
    html = """
    <html>
      <head><script>console.log("ignore js");</script></head>
      <body>
        <script type="application/json" data-sjs>{"require":[["ScheduledServerJS",[]]]}</script>
        <script type='application/json'>{"user":{"username":"yungis_"}}</script>
        <script type="application/json">{&quot;escaped&quot;:&quot;ok&quot;}</script>
        <script type="application/json">   </script>
      </body>
    </html>
    """
    payloads = extract_ssr_json_payloads(html)
    assert len(payloads) == 3
    assert payloads[0]["require"][0][0] == "ScheduledServerJS"
    assert payloads[1]["user"]["username"] == "yungis_"
    assert payloads[2]["escaped"] == "ok"


class _FakePlaywrightResponse:
    def __init__(self, url: str, status: int, body: str) -> None:
        self.url = url
        self.status = status
        self._body = body

    def text(self) -> str:
        return self._body


class _FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, fn) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit_response(self, resp: _FakePlaywrightResponse) -> None:
        for fn in self.handlers.get("response", []):
            fn(resp)


def test_response_collector_filtering_drain_and_safety_capture() -> None:
    page = _FakePage()
    collector = ResponseCollector()
    collector.attach(page)

    page.emit_response(_FakePlaywrightResponse("https://static.cdninstagram.com/app.js", 200, "js"))
    page.emit_response(
        _FakePlaywrightResponse(
            "https://www.instagram.com/graphql/query?doc_id=1",
            200,
            '{"a": 1}\n{"b": 2}',
        )
    )
    page.emit_response(
        _FakePlaywrightResponse("https://www.instagram.com/api/v1/media/1/comments/", 200, '{"comments": []}')
    )

    collector.raise_if_unsafe()
    assert len(collector.all_payloads()) == 3
    drained = collector.drain()
    assert len(drained) == 2
    assert collector.drain() == []

    page.emit_response(
        _FakePlaywrightResponse("https://www.instagram.com/graphql/query", 429, "Too Many Requests")
    )
    with pytest.raises(SafetyStopError) as exc:
        collector.raise_if_unsafe()
    assert exc.value.reason == "rate_limited"


def test_cooldown_tracker_deterministic_sleeps() -> None:
    recorded_sleeps: list[float] = []
    tracker = CooldownTracker(
        min_delay=1.5,
        max_delay=3.5,
        batch_size=3,
        cooldown_min=120.0,
        cooldown_max=180.0,
        sleep_fn=recorded_sleeps.append,
        rng=random.Random(0),
    )

    step1 = tracker.wait_step()
    assert 1.5 <= step1 <= 3.5

    assert tracker.record_item_completed() == 0.0
    assert tracker.record_item_completed() == 0.0
    cooldown = tracker.record_item_completed()
    assert 120.0 <= cooldown <= 180.0
    assert recorded_sleeps == [step1, cooldown]
