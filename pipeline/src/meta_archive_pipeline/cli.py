"""CLI entrypoint for Meta Archive desktop pipeline."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from meta_archive_pipeline import __version__

app = typer.Typer(
    name="meta-archive",
    help="Instagram & Facebook 개인 아카이브 수집·검증·빌드 CLI",
    no_args_is_help=True,
)
console = Console()


class IgPassChoice(StrEnum):
    """Selectable Instagram collection passes."""

    CORE = "core"
    LIKES = "likes"
    CONNECTIONS = "connections"
    TAGGED = "tagged"
    SAVED = "saved"


class FbPassChoice(StrEnum):
    """Selectable Facebook collection passes."""

    POSTS = "posts"
    FRIENDS = "friends"
    ALBUMS = "albums"


@app.callback()
def main() -> None:
    """Manage personal Instagram and Facebook archive pipeline."""


@app.command()
def version() -> None:
    """Print the installed meta-archive-pipeline version."""
    typer.echo(f"meta-archive {__version__}")


@app.command("launch-chrome")
def launch_chrome_cmd(
    profile_dir: Annotated[
        Path,
        typer.Option("--profile-dir", help="Chrome 전용 프로필 디렉터리"),
    ] = Path("workspace_data/chrome-profile"),
    port: Annotated[
        int,
        typer.Option("--port", help="localhost CDP 디버깅 포트"),
    ] = 9222,
    chrome_path: Annotated[
        Path | None,
        typer.Option("--chrome-path", help="Chrome 실행 파일 경로 (미지정 시 기본 경로 사용)"),
    ] = None,
    url: Annotated[
        str,
        typer.Option("--url", help="초기 접속 URL"),
    ] = "https://www.instagram.com/",
) -> None:
    """Launch local Chrome with a dedicated profile and localhost CDP port for manual login."""
    from meta_archive_pipeline.collect.cdp import launch_chrome

    proc = launch_chrome(
        profile_dir=profile_dir,
        port=port,
        chrome_path=chrome_path,
        start_url=url,
    )
    console.print(f"[green]Chrome 실행됨[/green] (PID={proc.pid}, CDP=http://127.0.0.1:{port})")
    console.print(f"전용 프로필: {profile_dir}")
    console.print("1. 열린 브라우저 창에서 수동으로 로그인(2단계 인증 포함)을 완료하세요.")
    console.print(
        "[yellow]주의: 모든 수집이 끝나면 보안상 전용 프로필 폴더(chrome-profile)를 반드시 삭제하세요.[/yellow]"
    )


@app.command()
def extract(
    zip_files: Annotated[
        list[Path],
        typer.Option("--zip", help="해제할 Meta 공식 ZIP 파일 경로 (여러 번 지정 가능)"),
    ],
    dest: Annotated[
        Path,
        typer.Option("--dest", help="병합 해제 대상 디렉터리"),
    ] = Path("workspace_data/official-raw"),
) -> None:
    """Merge-extract multiple independent Meta ZIP exports safely with Windows long-path support."""
    from meta_archive_pipeline.utils.zip_extract import extract_meta_zips

    report = extract_meta_zips(zip_files, dest)
    console.print(
        f"ZIP 처리={report.zip_files_processed}, 추출 파일={report.files_extracted}, "
        f"동일 중복 건너뜀={report.duplicates_identical}, 충돌={len(report.conflicts)}"
    )
    for conflict in report.conflicts:
        console.print(
            f"[yellow]충돌 보존:[/yellow] {conflict.rel_path} ({conflict.source_zip}) -> {conflict.saved_as}"
        )


@app.command("collect-ig")
def collect_ig_cmd(
    username: Annotated[
        str,
        typer.Option("--username", help="수집할 Instagram 사용자 이름"),
    ],
    collect_pass: Annotated[
        IgPassChoice,
        typer.Option("--pass", help="수집 패스 선택 (core/likes/connections/tagged/saved)"),
    ] = IgPassChoice.CORE,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="수집 데이터 저장 디렉터리"),
    ] = Path("workspace_data/collected/instagram"),
    cdp_url: Annotated[
        str,
        typer.Option("--cdp-url", help="실행 중인 Chrome CDP 주소"),
    ] = "http://127.0.0.1:9222",
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="스모크 테스트용 최대 수집 건수 제한"),
    ] = None,
    shortcodes: Annotated[
        str | None,
        typer.Option("--shortcodes", help="쉼표로 구분된 특정 shortcode 목록 (예: ABC,DEF)"),
    ] = None,
    download_avatars: Annotated[
        bool,
        typer.Option("--download-avatars/--no-download-avatars", help="프로필 사진 썸네일 저장 여부"),
    ] = True,
) -> None:
    """Collect Instagram data from a manually logged-in Chrome session via CDP."""
    from meta_archive_pipeline.collect.ig_collector import IgCollectConfig, collect_instagram

    parsed_shortcodes = (
        [s.strip() for s in shortcodes.split(",") if s.strip()] if shortcodes else None
    )
    config = IgCollectConfig(
        username=username,
        output_dir=output_dir,
        cdp_url=cdp_url,
        collect_pass=collect_pass.value,  # type: ignore[arg-type]
        limit=limit,
        shortcodes=parsed_shortcodes,
        download_avatars=download_avatars,
    )
    summary = collect_instagram(config)
    console.print(
        f"[IG {summary.collect_pass}] 처리={summary.processed}, 성공={summary.succeeded}, "
        f"실패={summary.failed}, 기존완료건너뜀={summary.skipped_done}"
    )
    if summary.paused_reason:
        console.print(f"[yellow]안전 일시중지:[/yellow] {summary.paused_reason}")


@app.command("collect-fb")
def collect_fb_cmd(
    collect_pass: Annotated[
        FbPassChoice,
        typer.Option("--pass", help="수집 패스 선택 (posts/friends/albums)"),
    ] = FbPassChoice.POSTS,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="수집 데이터 저장 디렉터리"),
    ] = Path("workspace_data/collected/facebook"),
    cdp_url: Annotated[
        str,
        typer.Option("--cdp-url", help="실행 중인 Chrome CDP 주소"),
    ] = "http://127.0.0.1:9222",
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="스모크 테스트용 최대 수집 건수 제한"),
    ] = None,
    download_avatars: Annotated[
        bool,
        typer.Option("--download-avatars/--no-download-avatars", help="친구 프로필 아바타 저장 여부"),
    ] = True,
) -> None:
    """Collect Facebook data from a manually logged-in Chrome session via CDP."""
    from meta_archive_pipeline.collect.fb_collector import FbCollectConfig, collect_facebook

    config = FbCollectConfig(
        output_dir=output_dir,
        cdp_url=cdp_url,
        collect_pass=collect_pass.value,  # type: ignore[arg-type]
        limit=limit,
        download_avatars=download_avatars,
    )
    summary = collect_facebook(config)
    console.print(
        f"[FB {summary.collect_pass}] 저장항목={summary.items_saved}, 미디어={summary.media_saved}"
    )
    if summary.paused_reason:
        console.print(f"[yellow]안전 일시중지:[/yellow] {summary.paused_reason}")


@app.command()
def verify(
    ig_official: Annotated[
        Path | None,
        typer.Option("--ig-official", help="해제된 Instagram 공식 JSON 디렉터리"),
    ] = None,
    fb_official: Annotated[
        Path | None,
        typer.Option("--fb-official", help="해제된 Facebook 공식 JSON 디렉터리"),
    ] = None,
    fb_e2ee: Annotated[
        Path | None,
        typer.Option("--fb-e2ee", help="Facebook 메신저 E2EE 백업 디렉터리"),
    ] = None,
    ig_collected: Annotated[
        Path | None,
        typer.Option("--ig-collected", help="Instagram CDP 수집 디렉터리"),
    ] = None,
    fb_collected: Annotated[
        Path | None,
        typer.Option("--fb-collected", help="Facebook CDP 수집 디렉터리"),
    ] = None,
    report_out: Annotated[
        Path | None,
        typer.Option("--report-out", help="검증 리포트 저장 경로 (.json 또는 .md)"),
    ] = None,
) -> None:
    """Audit official exports and collected outputs with ERROR/WARN severity gates."""
    from meta_archive_pipeline.utils.fs import atomic_write_bytes
    from meta_archive_pipeline.verify.auditor import audit_directories

    report = audit_directories(
        ig_official=ig_official,
        fb_official=fb_official,
        fb_e2ee=fb_e2ee,
        ig_collected=ig_collected,
        fb_collected=fb_collected,
    )

    gate_label = "[green]PASS[/green]" if report.passed_gate else "[red]FAIL[/red]"
    console.print(
        f"검증 판정: {gate_label} (ERROR={len(report.errors)}, WARN={len(report.warnings)})"
    )

    if report.cross_checks:
        table = Table(title="3중 교차 대조")
        table.add_column("플랫폼")
        table.add_column("라이브", justify="right")
        table.add_column("Playwright", justify="right")
        table.add_column("공식활성", justify="right")
        table.add_column("공식보관", justify="right")
        for cc in report.cross_checks:
            table.add_row(
                cc.platform,
                str(cc.live_profile_posts) if cc.live_profile_posts is not None else "-",
                str(cc.playwright_posts),
                str(cc.official_active_posts),
                str(cc.official_archived_posts),
            )
        console.print(table)

    for err in report.errors[:10]:
        console.print(f"[red][ERROR {err.code}][/red] {err.message}")
    for warn in report.warnings[:10]:
        console.print(f"[yellow][WARN {warn.code}][/yellow] {warn.message}")

    if report_out is not None:
        if report_out.suffix.lower() == ".json":
            content = json.dumps(report.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        else:
            content = report.format_markdown().encode("utf-8")
        atomic_write_bytes(report_out, content)
        console.print(f"리포트 저장: {report_out}")

    if not report.passed_gate:
        raise typer.Exit(code=1)


@app.command()
def build(
    ig_official: Annotated[
        Path | None,
        typer.Option("--ig-official", help="해제된 Instagram 공식 JSON 디렉터리"),
    ] = None,
    fb_official: Annotated[
        Path | None,
        typer.Option("--fb-official", help="해제된 Facebook 공식 JSON 디렉터리"),
    ] = None,
    fb_e2ee: Annotated[
        Path | None,
        typer.Option("--fb-e2ee", help="Facebook 메신저 E2EE 백업 디렉터리"),
    ] = None,
    ig_collected: Annotated[
        Path | None,
        typer.Option("--ig-collected", help="Instagram CDP 수집 디렉터리"),
    ] = None,
    fb_collected: Annotated[
        Path | None,
        typer.Option("--fb-collected", help="Facebook CDP 수집 디렉터리"),
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="최종 표준 아카이브 출력 디렉터리"),
    ] = Path("workspace_data/archive"),
    tz_offset: Annotated[
        str,
        typer.Option("--tz-offset", help="XMP 사이드카 타임존 오프셋"),
    ] = "+09:00",
) -> None:
    """Merge official exports and collected outputs, materialize media/XMP, build SQLite & manifest."""
    from meta_archive_pipeline.build.indexer import (
        build_sqlite_index,
        verify_manifest_sha256,
        write_manifest_sha256,
    )
    from meta_archive_pipeline.build.merger import materialize_archive, merge_all
    from meta_archive_pipeline.official import OfficialExportBundle
    from meta_archive_pipeline.official.fb_parser import parse_fb_official_export
    from meta_archive_pipeline.official.ig_parser import parse_ig_official_export
    from meta_archive_pipeline.schema.models import Connection, LikesInfo, Post, Profile, Story
    from meta_archive_pipeline.utils.fs import to_long_path
    from meta_archive_pipeline.verify.auditor import load_collected_directory

    bundles: list[OfficialExportBundle] = []
    source_roots: list[Path] = []
    if ig_official is not None:
        bundles.append(parse_ig_official_export(ig_official))
        source_roots.append(ig_official)
    if fb_official is not None:
        bundles.append(parse_fb_official_export(fb_official, e2ee_root=fb_e2ee))
        source_roots.append(fb_official)
        if fb_e2ee is not None:
            source_roots.append(fb_e2ee)

    collected_profiles: list[Profile] = []
    collected_posts: list[Post] = []
    collected_stories: list[Story] = []
    collected_conns: list[Connection] = []
    collected_roots: list[Path] = []

    for col_dir in (ig_collected, fb_collected):
        if col_dir is None or not col_dir.exists():
            continue
        collected_roots.append(col_dir)
        source_roots.append(col_dir)
        prof, posts, stories = load_collected_directory(col_dir)
        if prof is not None:
            collected_profiles.append(prof)

        likes_dir = col_dir / "likes"
        if likes_dir.exists():
            for post in posts:
                if post.shortcode:
                    lp = likes_dir / f"{post.shortcode}.json"
                    if lp.exists():
                        post.likes = LikesInfo.model_validate_json(
                            Path(to_long_path(lp)).read_text(encoding="utf-8")
                        )
        collected_posts.extend(posts)
        collected_stories.extend(stories)

        for conn_file in ("connections.json", "friends.json"):
            cp = col_dir / conn_file
            if cp.exists():
                raw_conns = json.loads(Path(to_long_path(cp)).read_text(encoding="utf-8"))
                if isinstance(raw_conns, list):
                    collected_conns.extend(Connection.model_validate(item) for item in raw_conns)

    merged = merge_all(
        official_bundles=bundles,
        collected_profiles=collected_profiles,
        collected_posts=collected_posts,
        collected_stories=collected_stories,
        collected_connections=collected_conns,
        collected_roots=collected_roots,
    )
    summary = materialize_archive(
        merged,
        output_dir,
        source_roots=source_roots,
        tz_offset=tz_offset,
    )
    idx_res = build_sqlite_index(output_dir)
    manifest_path, manifest_files = write_manifest_sha256(output_dir)
    checked, failures = verify_manifest_sha256(output_dir)

    console.print(
        f"[green]아카이브 빌드 완료[/green]: posts={summary.posts_count}, stories={summary.stories_count}, "
        f"connections={summary.connections_count}, threads={summary.threads_count}, "
        f"ambiguous={summary.ambiguous_count}"
    )
    console.print(
        f"미디어 복사={summary.media_copied}, XMP 생성={summary.xmp_written}, "
        f"DB={idx_res.db_path}, manifest={manifest_path} ({checked}/{manifest_files} verified)"
    )
    if failures:
        for fail in failures[:10]:
            console.print(f"[red]매니페스트 검증 실패:[/red] {fail}")
        raise typer.Exit(code=1)
