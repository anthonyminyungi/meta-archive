"""Tests for the meta-archive CLI entrypoint and Phase 2 commands."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meta_archive_pipeline import __version__
from meta_archive_pipeline.cli import app
from meta_archive_pipeline.collect.fb_collector import FbCollectSummary
from meta_archive_pipeline.collect.ig_collector import IgCollectSummary

runner = CliRunner()


def test_cli_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("version", "launch-chrome", "extract", "collect-ig", "collect-fb"):
        assert cmd in result.stdout
    assert "Instagram & Facebook" in result.stdout


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"meta-archive {__version__}" in result.stdout


@pytest.mark.parametrize("subcmd", ["launch-chrome", "extract", "collect-ig", "collect-fb"])
def test_subcommand_help(subcmd: str) -> None:
    result = runner.invoke(app, [subcmd, "--help"])
    assert result.exit_code == 0


def test_cli_extract_end_to_end(tmp_path: Path) -> None:
    zip_path = tmp_path / "export-1.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("media/posts/hello.jpg", b"jpeg-payload")

    dest = tmp_path / "extracted"
    result = runner.invoke(app, ["extract", "--zip", str(zip_path), "--dest", str(dest)])
    assert result.exit_code == 0
    assert "추출 파일=1" in result.stdout
    assert (dest / "media/posts/hello.jpg").read_bytes() == b"jpeg-payload"


def test_cli_collect_ig_validation_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bad = runner.invoke(app, ["collect-ig", "--username", "yungis_", "--pass", "invalid"])
    assert bad.exit_code != 0

    captured = []

    def fake_collect_instagram(cfg):
        captured.append(cfg)
        return IgCollectSummary(collect_pass=cfg.collect_pass, processed=2, succeeded=2)

    monkeypatch.setattr(
        "meta_archive_pipeline.collect.ig_collector.collect_instagram",
        fake_collect_instagram,
    )

    out_dir = tmp_path / "ig"
    res = runner.invoke(
        app,
        [
            "collect-ig",
            "--username",
            "yungis_",
            "--pass",
            "core",
            "--output-dir",
            str(out_dir),
            "--limit",
            "5",
            "--shortcodes",
            "SC1, SC2",
            "--no-download-avatars",
        ],
    )
    assert res.exit_code == 0
    assert len(captured) == 1
    cfg = captured[0]
    assert cfg.username == "yungis_"
    assert cfg.collect_pass == "core"
    assert cfg.output_dir == out_dir
    assert cfg.limit == 5
    assert cfg.shortcodes == ["SC1", "SC2"]
    assert cfg.download_avatars is False


def test_cli_collect_fb_validation_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bad = runner.invoke(app, ["collect-fb", "--pass", "wrong"])
    assert bad.exit_code != 0

    captured = []

    def fake_collect_facebook(cfg):
        captured.append(cfg)
        return FbCollectSummary(collect_pass=cfg.collect_pass, items_saved=3, media_saved=2)

    monkeypatch.setattr(
        "meta_archive_pipeline.collect.fb_collector.collect_facebook",
        fake_collect_facebook,
    )

    out_dir = tmp_path / "fb"
    res = runner.invoke(
        app,
        [
            "collect-fb",
            "--pass",
            "friends",
            "--output-dir",
            str(out_dir),
            "--limit",
            "10",
        ],
    )
    assert res.exit_code == 0
    assert len(captured) == 1
    cfg = captured[0]
    assert cfg.collect_pass == "friends"
    assert cfg.output_dir == out_dir
    assert cfg.limit == 10
    assert cfg.download_avatars is True
