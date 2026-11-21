"""CLI entrypoint for Meta Archive desktop pipeline."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

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
    console.print("[yellow]주의: 모든 수집이 끝나면 보안상 전용 프로필 폴더(chrome-profile)를 반드시 삭제하세요.[/yellow]")


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
