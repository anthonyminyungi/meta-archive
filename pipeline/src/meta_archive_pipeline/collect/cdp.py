"""Chrome 136+ CDP launcher, response interceptor, SSR/NDJSON parsers, and safety guards."""

from __future__ import annotations

import html as html_lib
import json
import os
import random
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from meta_archive_pipeline.utils.fs import to_long_path

DEFAULT_CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222

SafetyReason = Literal["rate_limited", "login_redirect", "checkpoint", "automated_behavior"]

_LOGIN_URL_MARKERS = ("/accounts/login", "facebook.com/login", "/login.php")
_CHECKPOINT_URL_MARKERS = ("/challenge/", "/checkpoint/")
_CHECKPOINT_BODY_MARKERS = (
    "checkpoint_required",
    "challenge_required",
    "temporarily locked",
    "계정이 일시적으로 잠겼",
)
_AUTOMATED_BODY_MARKERS = (
    "automated behavior",
    "자동화된 행동",
    "please wait a few minutes",
    "잠시 후 다시 시도",
)
_STRICT_PAGE_AUTOMATED_MARKERS = (
    "we suspect automated behavior",
    "automated behavior on your account",
    "자동화된 행동",
)
_SHORT_PAGE_TEXT_LIMIT = 1500
_SCRIPT_JSON_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']application/json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class SafetyStopError(RuntimeError):
    """Raised immediately when Meta returns a rate-limit, login redirect, or security challenge."""

    def __init__(
        self,
        reason: SafetyReason,
        url: str,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.url = url
        self.status = status
        self.detail = detail
        msg = f"Collection paused safely ({reason}) at {url}"
        if status is not None:
            msg += f" [HTTP {status}]"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


def resolve_chrome_path(chrome_path: Path | None = None) -> Path:
    """Resolve local Chrome executable from explicit path, CHROME_PATH env var, or default."""
    if chrome_path is not None:
        candidate = chrome_path
    elif env_path := os.environ.get("CHROME_PATH"):
        candidate = Path(env_path)
    else:
        candidate = DEFAULT_CHROME_PATH

    if not candidate.exists():
        raise FileNotFoundError(f"Chrome executable not found at: {candidate}")
    return candidate


def build_chrome_launch_args(
    chrome_path: Path,
    profile_dir: Path,
    *,
    port: int = DEFAULT_CDP_PORT,
    host: str = DEFAULT_CDP_HOST,
    start_url: str | None = None,
) -> list[str]:
    """Build Chrome CLI arguments for Chrome 136+ dedicated-profile CDP debugging."""
    resolved_profile = os.path.abspath(os.fspath(profile_dir))
    args = [
        os.fspath(chrome_path),
        f"--user-data-dir={resolved_profile}",
        f"--remote-debugging-address={host}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if start_url:
        args.append(start_url)
    return args


def launch_chrome(
    profile_dir: Path,
    *,
    port: int = DEFAULT_CDP_PORT,
    host: str = DEFAULT_CDP_HOST,
    chrome_path: Path | None = None,
    start_url: str | None = None,
) -> subprocess.Popen[bytes]:
    """Launch local Chrome with a dedicated profile and localhost CDP port."""
    os.makedirs(to_long_path(profile_dir), exist_ok=True)
    exe = resolve_chrome_path(chrome_path)
    args = build_chrome_launch_args(
        exe,
        profile_dir,
        port=port,
        host=host,
        start_url=start_url,
    )
    return subprocess.Popen(args)


def connect_cdp(
    playwright: Any,
    cdp_url: str = f"http://{DEFAULT_CDP_HOST}:{DEFAULT_CDP_PORT}",
) -> tuple[Any, Any]:
    """Attach Playwright to an already-running Chrome instance over CDP."""
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    contexts = getattr(browser, "contexts", None) or []
    context = contexts[0] if contexts else browser.new_context()
    return browser, context


def _response_has_error_envelope(body_text: str) -> bool:
    parsed = parse_ndjson_payload(body_text)
    if not parsed:
        return True
    return any(
        d.get("status") == "fail"
        or d.get("require_login") is True
        or bool(d.get("errors"))
        or bool(d.get("error"))
        for d in parsed
    )


def check_response_safety(url: str, status: int, body_text: str | None = None) -> None:
    """Inspect HTTP status, URL, and response body for Meta blocks or challenges."""
    if status == 429:
        raise SafetyStopError("rate_limited", url, status=status, detail="HTTP 429 rate limit")

    low_url = url.lower()
    if any(marker in low_url for marker in _LOGIN_URL_MARKERS):
        raise SafetyStopError(
            "login_redirect",
            url,
            status=status,
            detail="Redirected to login page",
        )
    if any(marker in low_url for marker in _CHECKPOINT_URL_MARKERS):
        raise SafetyStopError(
            "checkpoint",
            url,
            status=status,
            detail="Redirected to security checkpoint/challenge",
        )

    if body_text and (status >= 400 or _response_has_error_envelope(body_text)):
        low_body = body_text.lower()
        for marker in _CHECKPOINT_BODY_MARKERS:
            if marker in low_body:
                raise SafetyStopError(
                    "checkpoint",
                    url,
                    status=status,
                    detail=f"Matched checkpoint marker: {marker}",
                )
        for marker in _AUTOMATED_BODY_MARKERS:
            if marker in low_body:
                raise SafetyStopError(
                    "automated_behavior",
                    url,
                    status=status,
                    detail=f"Matched automated behavior marker: {marker}",
                )


def check_page_safety(page_url: str, body_text: str = "") -> None:
    """Check current browser page URL and visible text for login/checkpoint/rate-limit states."""
    check_response_safety(page_url, 200, None)
    if not body_text:
        return
    low_body = body_text.lower()
    for marker in _CHECKPOINT_BODY_MARKERS:
        if marker in low_body:
            raise SafetyStopError(
                "checkpoint",
                page_url,
                status=200,
                detail=f"Matched checkpoint marker: {marker}",
            )
    markers = (
        _AUTOMATED_BODY_MARKERS
        if len(body_text.strip()) <= _SHORT_PAGE_TEXT_LIMIT
        else _STRICT_PAGE_AUTOMATED_MARKERS
    )
    for marker in markers:
        if marker in low_body:
            raise SafetyStopError(
                "automated_behavior",
                page_url,
                status=200,
                detail=f"Matched automated behavior marker: {marker}",
            )


def _strip_xssi_prefix(text: str) -> str:
    cleaned = text.lstrip()
    if cleaned.startswith("for (;;);"):
        return cleaned[len("for (;;);") :].lstrip()
    return cleaned


def _collect_dict_items(parsed: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(parsed, dict):
        out.append(parsed)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                out.append(item)


def parse_ndjson_payload(raw_text: str) -> list[dict[str, Any]]:
    """Parse Meta XSSI-prefixed JSON, JSON arrays, or multi-chunk Relay NDJSON payloads."""
    cleaned = _strip_xssi_prefix(raw_text)
    if not cleaned:
        return []

    out: list[dict[str, Any]] = []
    try:
        parsed = json.loads(cleaned)
        _collect_dict_items(parsed, out)
        return out
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for raw_line in raw_text.splitlines():
        line = _strip_xssi_prefix(raw_line).strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            _collect_dict_items(parsed, out)
            continue
        except json.JSONDecodeError:
            pass

        idx = 0
        length = len(line)
        while idx < length:
            while idx < length and line[idx].isspace():
                idx += 1
            if idx >= length or line[idx] not in "{[":
                break
            try:
                parsed, next_idx = decoder.raw_decode(line, idx)
            except json.JSONDecodeError:
                break
            _collect_dict_items(parsed, out)
            idx = next_idx

    return out


def extract_ssr_json_payloads(html: str) -> list[dict[str, Any]]:
    """Extract embedded JSON payloads from `<script type="application/json">` SSR tags."""
    payloads: list[dict[str, Any]] = []
    for match in _SCRIPT_JSON_RE.finditer(html):
        body = match.group(1).strip()
        if not body:
            continue
        parsed = parse_ndjson_payload(body)
        if not parsed and ("&quot;" in body or "&#" in body):
            parsed = parse_ndjson_payload(html_lib.unescape(body))
        payloads.extend(parsed)
    return payloads


@dataclass(frozen=True)
class CapturedResponse:
    """Single intercepted API/GraphQL response and its parsed JSON objects."""

    url: str
    status: int
    raw_text: str
    payloads: list[dict[str, Any]]


class ResponseCollector:
    """Intercepts matching Playwright network responses and enforces safety stops."""

    def __init__(
        self,
        url_substrings: Sequence[str] = ("/graphql/query", "/api/graphql", "/graphql", "/api/v1/"),
    ) -> None:
        self.url_substrings = tuple(url_substrings)
        self.responses: list[CapturedResponse] = []
        self.safety_error: SafetyStopError | None = None

    def attach(self, page: Any) -> None:
        """Attach response listener to a Playwright Page."""
        page.on("response", self.handle_response)

    def handle_response(self, response: Any) -> None:
        """Handle a single Playwright Response event."""
        url = str(getattr(response, "url", ""))
        if not any(sub in url for sub in self.url_substrings):
            return

        status = int(getattr(response, "status", 0))
        raw_text = ""
        try:
            text_attr = getattr(response, "text", None)
            if callable(text_attr):
                raw_text = text_attr() or ""
            elif isinstance(text_attr, str):
                raw_text = text_attr
        except Exception:
            raw_text = ""

        try:
            check_response_safety(url, status, raw_text)
        except SafetyStopError as exc:
            if self.safety_error is None:
                self.safety_error = exc
            return

        payloads = parse_ndjson_payload(raw_text)
        self.responses.append(
            CapturedResponse(
                url=url,
                status=status,
                raw_text=raw_text,
                payloads=payloads,
            )
        )

    def raise_if_unsafe(self) -> None:
        """Raise stored SafetyStopError if an unsafe response was intercepted."""
        if self.safety_error is not None:
            raise self.safety_error

    def drain(self) -> list[CapturedResponse]:
        """Return and clear all captured responses accumulated so far."""
        drained = list(self.responses)
        self.responses.clear()
        return drained

    def all_payloads(self) -> list[dict[str, Any]]:
        """Return flattened parsed JSON dicts from currently retained responses."""
        out: list[dict[str, Any]] = []
        for item in self.responses:
            out.extend(item.payloads)
        return out


@dataclass
class CooldownTracker:
    """Human-like step jitter and periodic batch cooldowns to avoid Meta rate limits."""

    min_delay: float = 1.5
    max_delay: float = 3.5
    batch_size: int = 45
    cooldown_min: float = 120.0
    cooldown_max: float = 180.0
    sleep_fn: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    items_completed: int = 0

    def wait_step(self) -> float:
        """Sleep a random interval between individual UI/navigation actions."""
        if self.max_delay <= 0:
            return 0.0
        delay = self.rng.uniform(self.min_delay, self.max_delay)
        if delay > 0:
            self.sleep_fn(delay)
        return delay

    def record_item_completed(self) -> float:
        """Record a completed item and trigger batch cooldown every `batch_size` items."""
        self.items_completed += 1
        if self.batch_size > 0 and self.items_completed % self.batch_size == 0:
            if self.cooldown_max <= 0:
                return 0.0
            delay = self.rng.uniform(self.cooldown_min, self.cooldown_max)
            if delay > 0:
                self.sleep_fn(delay)
            return delay
        return 0.0
