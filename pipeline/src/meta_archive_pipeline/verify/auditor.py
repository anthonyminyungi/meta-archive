"""Severity-based integrity auditor (ERROR/WARN gate, timeline histogram, 3-way cross-check)."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from meta_archive_pipeline.official import OfficialExportBundle
from meta_archive_pipeline.official.fb_parser import parse_fb_official_export
from meta_archive_pipeline.official.ig_parser import parse_ig_official_export
from meta_archive_pipeline.schema.models import MediaItem, Post, Profile, Story
from meta_archive_pipeline.utils.fs import to_long_path
from meta_archive_pipeline.utils.media import inspect_mp4, verify_image

AuditSeverity = Literal["ERROR", "WARN"]

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
_NON_MEDIA_SCAN_EXTS = {".json", ".html", ".htm", ".txt", ".md", ".csv"}
_IGNORED_FILENAMES = {".ds_store", "thumbs.db", "desktop.ini"}


@dataclass(frozen=True)
class AuditIssue:
    """Single verification finding classified by severity."""

    severity: AuditSeverity
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrossCheckSummary:
    """Comparison of post counts across live profile header, Playwright, and official JSON."""

    platform: str
    live_profile_posts: int | None
    playwright_posts: int
    official_active_posts: int
    official_archived_posts: int
    delta_live_vs_playwright: int | None
    delta_official_vs_playwright: int
    official_timeline_posts: int | None = None
    official_nontimeline_posts: int | None = None


@dataclass
class AuditReport:
    """Complete pre-deletion integrity audit report."""

    errors: list[AuditIssue] = field(default_factory=list)
    warnings: list[AuditIssue] = field(default_factory=list)
    year_month_histogram: dict[str, dict[str, int]] = field(default_factory=dict)
    zero_month_gaps: dict[str, list[str]] = field(default_factory=dict)
    cross_checks: list[CrossCheckSummary] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def passed_gate(self) -> bool:
        """Return True when zero fatal ERROR issues remain."""
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report into a JSON-compatible dictionary."""
        return {
            "passed_gate": self.passed_gate,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [
                {
                    "severity": e.severity,
                    "code": e.code,
                    "message": e.message,
                    "path": e.path,
                    "details": e.details,
                }
                for e in self.errors
            ],
            "warnings": [
                {
                    "severity": w.severity,
                    "code": w.code,
                    "message": w.message,
                    "path": w.path,
                    "details": w.details,
                }
                for w in self.warnings
            ],
            "year_month_histogram": self.year_month_histogram,
            "zero_month_gaps": self.zero_month_gaps,
            "cross_checks": [
                {
                    "platform": c.platform,
                    "live_profile_posts": c.live_profile_posts,
                    "playwright_posts": c.playwright_posts,
                    "official_active_posts": c.official_active_posts,
                    "official_archived_posts": c.official_archived_posts,
                    "official_timeline_posts": c.official_timeline_posts,
                    "official_nontimeline_posts": c.official_nontimeline_posts,
                    "delta_live_vs_playwright": c.delta_live_vs_playwright,
                    "delta_official_vs_playwright": c.delta_official_vs_playwright,
                }
                for c in self.cross_checks
            ],
            "stats": self.stats,
        }

    def format_markdown(self) -> str:
        """Render a concise human-readable Markdown audit report."""
        lines = [
            "# Meta Archive 무결성 검증 리포트",
            "",
            f"- **게이트 판정:** {'PASS (ERROR 0건)' if self.passed_gate else f'FAIL (ERROR {len(self.errors)}건)'}",
            f"- **ERROR:** {len(self.errors)}건 / **WARN:** {len(self.warnings)}건",
            "",
        ]
        if self.cross_checks:
            lines.append("## 3중 교차 대조")
            lines.append("| 플랫폼 | 라이브 프로필 | Playwright | 공식 활성 | 공식 보관 | 라이브-수집 차 |")
            lines.append("| :--- | ---: | ---: | ---: | ---: | ---: |")
            for cc in self.cross_checks:
                live_s = str(cc.live_profile_posts) if cc.live_profile_posts is not None else "-"
                delta_s = (
                    str(cc.delta_live_vs_playwright)
                    if cc.delta_live_vs_playwright is not None
                    else "-"
                )
                lines.append(
                    f"| {cc.platform} | {live_s} | {cc.playwright_posts} | "
                    f"{cc.official_active_posts} | {cc.official_archived_posts} | {delta_s} |"
                )
            lines.append("")

        if self.errors:
            lines.append("## ERROR 목록 (계정 삭제 전 해결 필수)")
            for err in self.errors:
                lines.append(f"- `[{err.code}]` {err.message}" + (f" (`{err.path}`)" if err.path else ""))
            lines.append("")

        if self.warnings:
            lines.append("## WARN 목록 (사유 검토 필요)")
            for warn in self.warnings:
                lines.append(f"- `[{warn.code}]` {warn.message}" + (f" (`{warn.path}`)" if warn.path else ""))
            lines.append("")

        if self.zero_month_gaps:
            lines.append("## 타임라인 0건 공백 구간 (3개월 이상)")
            for series, gaps in sorted(self.zero_month_gaps.items()):
                lines.append(f"- `{series}`: {', '.join(gaps)}")
            lines.append("")

        return "\n".join(lines)


def load_collected_directory(
    collected_dir: Path,
) -> tuple[Profile | None, list[Post], list[Story]]:
    """Load normalized Profile, Posts, and Stories from a Phase 2 collector output directory."""
    profile: Profile | None = None
    prof_path = collected_dir / "profile.json"
    if prof_path.exists():
        profile = Profile.model_validate_json(
            Path(to_long_path(prof_path)).read_text(encoding="utf-8")
        )

    posts: list[Post] = []
    for subdir in ("posts", "tagged_posts"):
        posts_dir = collected_dir / subdir
        if posts_dir.exists():
            for pf in sorted(posts_dir.glob("*.json")):
                posts.append(
                    Post.model_validate_json(Path(to_long_path(pf)).read_text(encoding="utf-8"))
                )

    stories: list[Story] = []
    hl_path = collected_dir / "highlights.json"
    if hl_path.exists():
        raw_hl = json.loads(Path(to_long_path(hl_path)).read_text(encoding="utf-8"))
        if isinstance(raw_hl, list):
            for entry in raw_hl:
                stories.append(Story.model_validate(entry))

    return profile, posts, stories


def _resolve_media_path(
    rel_path: str,
    candidate_roots: Sequence[Path],
) -> tuple[Path | None, Path | None]:
    """Resolve normalized relative path against official or collector root layouts."""
    rel_parts = rel_path.split("/")
    alt_parts: list[str] | None = None
    if len(rel_parts) >= 3 and rel_parts[0] == "media" and rel_parts[1] in ("instagram", "facebook"):
        alt_parts = ["media", *rel_parts[2:]]

    for root in candidate_roots:
        direct = root.joinpath(*rel_parts)
        if os.path.exists(to_long_path(direct)):
            return direct, root
        if alt_parts is not None:
            alt = root.joinpath(*alt_parts)
            if os.path.exists(to_long_path(alt)):
                return alt, root
    return None, None


def _check_json_files_under(root: Path, issues: list[AuditIssue]) -> int:
    count = 0
    if not root.exists():
        return 0
    for jf in sorted(root.rglob("*.json")):
        count += 1
        try:
            with open(to_long_path(jf), "rb") as handle:
                json.loads(handle.read().decode("utf-8"))
        except Exception as exc:
            rel = jf.relative_to(root).as_posix()
            issues.append(
                AuditIssue(
                    severity="ERROR",
                    code="JSON_PARSE_ERROR",
                    message=f"JSON 파싱 실패: {exc}",
                    path=rel,
                )
            )
    return count


def _build_continuous_histogram(timestamps: Sequence[int]) -> tuple[dict[str, int], list[str]]:
    if not timestamps:
        return {}, []
    counts: Counter[tuple[int, int]] = Counter()
    for ts in timestamps:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        counts[(dt.year, dt.month)] += 1

    min_ym = min(counts)
    max_ym = max(counts)
    histogram: dict[str, int] = {}
    gap_months: list[str] = []
    zero_run: list[str] = []

    y, m = min_ym
    while (y, m) <= max_ym:
        label = f"{y:04d}-{m:02d}"
        cnt = counts.get((y, m), 0)
        histogram[label] = cnt
        if cnt == 0:
            zero_run.append(label)
        else:
            if len(zero_run) >= 3:
                gap_months.append(f"{zero_run[0]}..{zero_run[-1]} ({len(zero_run)}m)")
            zero_run = []

        if m == 12:
            y += 1
            m = 1
        else:
            m += 1

    if len(zero_run) >= 3:
        gap_months.append(f"{zero_run[0]}..{zero_run[-1]} ({len(zero_run)}m)")

    return histogram, gap_months


def audit_sources(
    *,
    official_bundles: Sequence[OfficialExportBundle] = (),
    collected_profiles: Sequence[Profile] = (),
    collected_posts: Sequence[Post] = (),
    collected_stories: Sequence[Story] = (),
    collected_roots: Sequence[Path] = (),
    json_scan_roots: Sequence[Path] = (),
) -> AuditReport:
    """Run full ERROR/WARN integrity audit across parsed official bundles and collected outputs."""
    report = AuditReport()
    all_issues: list[AuditIssue] = []

    # 1. Scan JSON syntax health on disk
    scan_roots: list[Path] = list(json_scan_roots)
    for b in official_bundles:
        scan_roots.append(b.root_dir)
        scan_roots.extend(b.extra_roots)
    scan_roots.extend(collected_roots)
    seen_roots: set[Path] = set()
    json_checked = 0
    for root in scan_roots:
        resolved = root.resolve()
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        json_checked += _check_json_files_under(root, all_issues)

    # 2. Verify referenced media items and track referenced official paths
    referenced_by_root: dict[Path, set[str]] = {}
    checked_paths: set[str] = set()
    media_checked = 0

    def _audit_media_item(
        media: MediaItem,
        candidate_roots: Sequence[Path],
        context_label: str,
    ) -> None:
        nonlocal media_checked
        if not media.local_path:
            if media.original_uri and media.original_uri.startswith(("http://", "https://")):
                return
            all_issues.append(
                AuditIssue(
                    severity="WARN",
                    code="EMPTY_MEDIA_URI",
                    message=f"{context_label}의 미디어 URI가 비어 있습니다.",
                )
            )
            return

        resolved_file, matched_root = _resolve_media_path(media.local_path, candidate_roots)
        if resolved_file is None or matched_root is None:
            all_issues.append(
                AuditIssue(
                    severity="ERROR",
                    code="MISSING_MEDIA",
                    message=f"참조된 로컬 미디어 파일이 존재하지 않습니다: {media.local_path}",
                    path=media.local_path,
                )
            )
            return

        rel_in_root = resolved_file.relative_to(matched_root).as_posix()
        referenced_by_root.setdefault(matched_root.resolve(), set()).add(rel_in_root)

        key = to_long_path(resolved_file)
        if key in checked_paths:
            return
        checked_paths.add(key)
        media_checked += 1

        try:
            byte_size = os.path.getsize(key)
        except OSError as exc:
            all_issues.append(
                AuditIssue(
                    severity="ERROR",
                    code="MISSING_MEDIA",
                    message=f"파일 크기 조회 실패: {exc}",
                    path=media.local_path,
                )
            )
            return

        if byte_size == 0:
            all_issues.append(
                AuditIssue(
                    severity="ERROR",
                    code="ZERO_BYTE_FILE",
                    message=f"0바이트 미디어 파일입니다: {media.local_path}",
                    path=media.local_path,
                )
            )
            return

        ext = resolved_file.suffix.lower()
        if media.media_type == "audio":
            if ext in _VIDEO_EXTS or ext == ".m4a":
                mp4_res = inspect_mp4(resolved_file)
                if not mp4_res.ok:
                    all_issues.append(
                        AuditIssue(
                            severity="ERROR",
                            code="CORRUPT_AUDIO",
                            message=f"손상된 오디오 컨테이너 파일: {mp4_res.error}",
                            path=media.local_path,
                        )
                    )
                elif not mp4_res.has_audio:
                    all_issues.append(
                        AuditIssue(
                            severity="WARN",
                            code="AUDIO_NO_STREAM",
                            message=f"오디오 트랙(soun)이 없는 음성 파일입니다: {media.local_path}",
                            path=media.local_path,
                        )
                    )
        elif media.media_type == "image" or ext in _IMAGE_EXTS:
            img_res = verify_image(resolved_file)
            if not img_res.ok:
                all_issues.append(
                    AuditIssue(
                        severity="ERROR",
                        code="CORRUPT_IMAGE",
                        message=f"손상된 이미지 파일: {img_res.error}",
                        path=media.local_path,
                    )
                )
        elif media.media_type == "video" or ext in _VIDEO_EXTS:
            mp4_res = inspect_mp4(resolved_file)
            if not mp4_res.ok:
                all_issues.append(
                    AuditIssue(
                        severity="ERROR",
                        code="CORRUPT_VIDEO",
                        message=f"손상된 영상 파일: {mp4_res.error}",
                        path=media.local_path,
                    )
                )
            elif not mp4_res.has_audio and media.is_audio_muted is not True:
                all_issues.append(
                    AuditIssue(
                        severity="WARN",
                        code="VIDEO_NO_AUDIO",
                        message=f"오디오 트랙(soun)이 없는 영상 파일입니다: {media.local_path}",
                        path=media.local_path,
                    )
                )

    for bundle in official_bundles:
        roots = [bundle.root_dir, *bundle.extra_roots]
        if bundle.profile and bundle.profile.avatar_local_path:
            _audit_media_item(
                MediaItem(media_type="image", local_path=bundle.profile.avatar_local_path),
                roots,
                f"{bundle.platform} profile",
            )
        for post in bundle.posts:
            for m in post.media:
                _audit_media_item(m, roots, f"{bundle.platform} post {post.post_id}")
        for story in bundle.stories:
            if story.media is None:
                all_issues.append(
                    AuditIssue(
                        severity="WARN",
                        code="EMPTY_MEDIA_URI",
                        message=f"{bundle.platform} 스토리 {story.story_id}의 미디어 URI가 비어 있습니다.",
                    )
                )
            else:
                _audit_media_item(story.media, roots, f"{bundle.platform} story {story.story_id}")
        for thread in bundle.messages:
            if thread.avatar_local_path:
                _audit_media_item(
                    MediaItem(media_type="image", local_path=thread.avatar_local_path),
                    roots,
                    f"{bundle.platform} thread {thread.thread_id} avatar",
                )
            for msg in thread.messages:
                for m in msg.media:
                    _audit_media_item(m, roots, f"{bundle.platform} thread {thread.thread_id}")
        for act in bundle.activities:
            for m in act.media:
                _audit_media_item(m, roots, f"{bundle.platform} activity {act.activity_id}")

    if collected_roots:
        for profile in collected_profiles:
            if profile.avatar_local_path:
                _audit_media_item(
                    MediaItem(media_type="image", local_path=profile.avatar_local_path),
                    collected_roots,
                    f"{profile.platform} collected profile",
                )
        for post in collected_posts:
            if post.shared_post and post.shared_post.thumbnail_local_path:
                _audit_media_item(
                    MediaItem(media_type="image", local_path=post.shared_post.thumbnail_local_path),
                    collected_roots,
                    f"{post.platform} collected post {post.post_id} link thumbnail",
                )
            for m in post.media:
                _audit_media_item(m, collected_roots, f"{post.platform} collected post {post.post_id}")
        for story in collected_stories:
            if story.highlight_cover:
                _audit_media_item(
                    MediaItem(media_type="image", local_path=story.highlight_cover),
                    collected_roots,
                    f"{story.platform} collected story {story.story_id} highlight cover",
                )
            if story.media is not None:
                _audit_media_item(
                    story.media, collected_roots, f"{story.platform} collected story {story.story_id}"
                )

    # 3. Orphan media/binary files across the entire official export tree
    for bundle in official_bundles:
        if not bundle.root_dir.exists():
            continue
        refs = referenced_by_root.get(bundle.root_dir.resolve(), set())
        for mf in sorted(bundle.root_dir.rglob("*")):
            if not mf.is_file():
                continue
            if mf.name.lower() in _IGNORED_FILENAMES or mf.suffix.lower() in _NON_MEDIA_SCAN_EXTS:
                continue
            rel = mf.relative_to(bundle.root_dir).as_posix()
            if rel not in refs:
                all_issues.append(
                    AuditIssue(
                        severity="WARN",
                        code="ORPHAN_MEDIA",
                        message=f"JSON에서 참조되지 않는 고아 미디어 파일입니다: {rel}",
                        path=rel,
                    )
                )

    # 3b. Detect Facebook Messenger E2EE cutoff when no external `--fb-e2ee` export is supplied
    for bundle in official_bundles:
        if bundle.platform != "facebook" or bundle.extra_roots:
            continue
        has_e2ee_threads = any(t.is_e2ee for t in bundle.messages)
        if has_e2ee_threads:
            continue

        armadillo_count = 0
        for rel_sec in (
            "your_facebook_activity/messages/secret_conversations.json",
            "messages/secret_conversations.json",
        ):
            sec_path = bundle.root_dir.joinpath(*rel_sec.split("/"))
            if sec_path.exists():
                try:
                    with open(to_long_path(sec_path), "rb") as handle:
                        sec_data = json.loads(handle.read().decode("utf-8"))
                    sc2 = (
                        sec_data.get("secret_conversations_v2")
                        if isinstance(sec_data, dict)
                        else None
                    )
                    if isinstance(sc2, dict) and isinstance(sc2.get("armadillo_devices"), list):
                        armadillo_count = len(sc2["armadillo_devices"])
                except Exception:
                    pass
                break

        max_msg_ts = max(
            (m.timestamp_unix or 0 for t in bundle.messages for m in t.messages),
            default=0,
        )
        max_other_ts = max(
            [
                *(p.timestamp_unix or 0 for p in bundle.posts),
                *(a.timestamp_unix or 0 for a in bundle.activities),
            ],
            default=0,
        )
        trailing_days = (max_other_ts - max_msg_ts) // 86400 if (max_msg_ts and max_other_ts > max_msg_ts) else 0
        if armadillo_count > 0 or trailing_days >= 180:
            all_issues.append(
                AuditIssue(
                    severity="WARN",
                    code="FB_E2EE_CUTOFF_SUSPECTED",
                    message=(
                        f"Facebook 메신저 E2EE 기기({armadillo_count}대) 또는 메시지 조기 중단({trailing_days}일 차이)이 "
                        "감지되었습니다. 브라우저 보안 저장소(PIN)에 암호화 대화가 있다면 `--fb-e2ee` 백업을 확인하세요."
                    ),
                    details={"armadillo_devices": armadillo_count, "trailing_days": trailing_days},
                )
            )

    # 4. Year x Month continuous histograms & 3+ month zero gaps
    series_timestamps: dict[str, list[int]] = {}
    for bundle in official_bundles:
        key_posts = f"{bundle.platform}:posts"
        for p in bundle.posts:
            if p.timestamp_unix:
                series_timestamps.setdefault(key_posts, []).append(p.timestamp_unix)
        key_stories = f"{bundle.platform}:stories"
        for s in bundle.stories:
            if s.timestamp_unix:
                series_timestamps.setdefault(key_stories, []).append(s.timestamp_unix)
        key_msgs = f"{bundle.platform}:messages"
        for t in bundle.messages:
            for msg in t.messages:
                if msg.timestamp_unix:
                    series_timestamps.setdefault(key_msgs, []).append(msg.timestamp_unix)

    for p in collected_posts:
        if p.timestamp_unix and p.post_type != "tagged":
            series_timestamps.setdefault(f"{p.platform}:collected_posts", []).append(p.timestamp_unix)

    for series_name, ts_list in sorted(series_timestamps.items()):
        hist, gaps = _build_continuous_histogram(ts_list)
        report.year_month_histogram[series_name] = hist
        if gaps:
            report.zero_month_gaps[series_name] = gaps
            all_issues.append(
                AuditIssue(
                    severity="WARN",
                    code="TIMELINE_GAP",
                    message=f"{series_name} 타임라인에 3개월 이상 0건 공백 구간이 있습니다: {', '.join(gaps)}",
                    details={"series": series_name, "gaps": gaps},
                )
            )

    # 5. 3-way Cross Check (Live profile counts vs Playwright collected vs Official JSON)
    platforms = sorted(
        {
            *(b.platform for b in official_bundles),
            *(p.platform for p in collected_profiles),
            *(p.platform for p in collected_posts),
        }
    )
    for platform in platforms:
        live_prof = next((p for p in collected_profiles if p.platform == platform), None)
        live_count = live_prof.counts.posts if live_prof else None
        pw_count = sum(
            1 for p in collected_posts if p.platform == platform and p.post_type != "tagged"
        )
        plat_off_posts = [
            p for b in official_bundles if b.platform == platform for p in b.posts
        ]
        off_active = sum(1 for p in plat_off_posts if not p.is_archived)
        off_arch = sum(1 for p in plat_off_posts if p.is_archived)

        off_timeline: int | None = None
        off_nontimeline: int | None = None
        if platform == "facebook":
            off_timeline = sum(
                1
                for p in plat_off_posts
                if not p.is_archived
                and not p.post_id.startswith(("fb_album_", "fb_photo_", "fb_video_", "fb_group_"))
            )
            off_nontimeline = off_active - off_timeline

        has_official = any(b.platform == platform for b in official_bundles)
        delta_live = (live_count - pw_count) if live_count is not None else None
        comparable_off = off_timeline if (platform == "facebook" and off_timeline is not None) else off_active
        delta_off = comparable_off - pw_count

        cc = CrossCheckSummary(
            platform=platform,
            live_profile_posts=live_count,
            playwright_posts=pw_count,
            official_active_posts=off_active,
            official_archived_posts=off_arch,
            delta_live_vs_playwright=delta_live,
            delta_official_vs_playwright=delta_off,
            official_timeline_posts=off_timeline,
            official_nontimeline_posts=off_nontimeline,
        )
        report.cross_checks.append(cc)

        if (delta_live is not None and delta_live != 0) or (has_official and pw_count > 0 and delta_off != 0):
            all_issues.append(
                AuditIssue(
                    severity="WARN",
                    code="COUNT_MISMATCH",
                    message=(
                        f"{platform} 게시물 수량 차이: 라이브={live_count}, "
                        f"Playwright={pw_count}, 공식활성={off_active}, 공식보관={off_arch}"
                    ),
                    details={
                        "platform": platform,
                        "official_timeline_posts": off_timeline,
                        "official_nontimeline_posts": off_nontimeline,
                    },
                )
            )

    report.errors = [i for i in all_issues if i.severity == "ERROR"]
    report.warnings = [i for i in all_issues if i.severity == "WARN"]
    report.stats = {
        "json_files_checked": json_checked,
        "unique_media_checked": media_checked,
        "official_posts": sum(len(b.posts) for b in official_bundles),
        "official_stories": sum(len(b.stories) for b in official_bundles),
        "official_threads": sum(len(b.messages) for b in official_bundles),
        "official_activities": sum(len(b.activities) for b in official_bundles),
        "collected_posts": len(collected_posts),
        "collected_tagged_posts": sum(1 for p in collected_posts if p.post_type == "tagged"),
    }
    return report


def audit_directories(
    *,
    ig_official: Path | None = None,
    fb_official: Path | None = None,
    fb_e2ee: Path | None = None,
    ig_collected: Path | None = None,
    fb_collected: Path | None = None,
) -> AuditReport:
    """Parse official and collected directories on disk and run `audit_sources`."""
    bundles: list[OfficialExportBundle] = []
    json_scan_roots: list[Path] = []
    load_errors: list[AuditIssue] = []

    for flag_label, cand_dir in (
        ("--ig-official", ig_official),
        ("--fb-official", fb_official),
        ("--fb-e2ee", fb_e2ee),
        ("--ig-collected", ig_collected),
        ("--fb-collected", fb_collected),
    ):
        if cand_dir is not None and not os.path.isdir(to_long_path(cand_dir)):
            load_errors.append(
                AuditIssue(
                    severity="ERROR",
                    code="MISSING_SOURCE_DIR",
                    message=f"지정된 입력 디렉터리를 찾을 수 없거나 접근할 수 없습니다 ({flag_label}): {cand_dir}",
                    path=str(cand_dir),
                )
            )

    if ig_official is not None and os.path.isdir(to_long_path(ig_official)):
        json_scan_roots.append(ig_official)
        try:
            bundles.append(parse_ig_official_export(ig_official))
        except Exception as exc:
            load_errors.append(
                AuditIssue(
                    severity="ERROR",
                    code="JSON_PARSE_ERROR",
                    message=f"IG 공식 반출본 파싱 실패: {exc}",
                    path=str(ig_official),
                )
            )

    if fb_official is not None and os.path.isdir(to_long_path(fb_official)):
        json_scan_roots.append(fb_official)
        if fb_e2ee is not None and os.path.isdir(to_long_path(fb_e2ee)):
            json_scan_roots.append(fb_e2ee)
        try:
            bundles.append(parse_fb_official_export(fb_official, e2ee_root=fb_e2ee))
        except Exception as exc:
            load_errors.append(
                AuditIssue(
                    severity="ERROR",
                    code="JSON_PARSE_ERROR",
                    message=f"FB 공식 반출본 파싱 실패: {exc}",
                    path=str(fb_official),
                )
            )

    collected_profiles: list[Profile] = []
    collected_posts: list[Post] = []
    collected_stories: list[Story] = []
    collected_roots: list[Path] = []

    for col_dir in (ig_collected, fb_collected):
        if col_dir is None or not col_dir.exists():
            continue
        collected_roots.append(col_dir)
        prof, posts, stories = load_collected_directory(col_dir)
        if prof is not None:
            collected_profiles.append(prof)
        collected_posts.extend(posts)
        collected_stories.extend(stories)

    report = audit_sources(
        official_bundles=bundles,
        collected_profiles=collected_profiles,
        collected_posts=collected_posts,
        collected_stories=collected_stories,
        collected_roots=collected_roots,
        json_scan_roots=json_scan_roots,
    )
    if load_errors:
        # Prepend parse errors while avoiding duplicate messages if JSON scan already caught them
        existing = {(e.code, e.path) for e in report.errors}
        for err in load_errors:
            if (err.code, err.path) not in existing:
                report.errors.insert(0, err)
    return report
