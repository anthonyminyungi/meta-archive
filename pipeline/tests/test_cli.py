"""Tests for the meta-archive CLI entrypoint and Phase 2 & 3 commands."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from meta_archive_pipeline import __version__
from meta_archive_pipeline.cli import app
from meta_archive_pipeline.collect.fb_collector import FbCollectSummary
from meta_archive_pipeline.collect.ig_collector import IgCollectSummary
from meta_archive_pipeline.schema.models import Connection, LikesInfo, MediaItem, Post, UserRef
from meta_archive_pipeline.schema.paths import ArchivePaths

runner = CliRunner()


def test_cli_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("version", "launch-chrome", "extract", "collect-ig", "collect-fb", "verify", "build"):
        assert cmd in result.stdout
    assert "Instagram & Facebook" in result.stdout


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"meta-archive {__version__}" in result.stdout


@pytest.mark.parametrize(
    "subcmd",
    ["launch-chrome", "extract", "collect-ig", "collect-fb", "verify", "build"],
)
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


def _make_mini_ig_official(root: Path, *, include_media_file: bool = True) -> None:
    if include_media_file:
        img = root / "media" / "posts" / "101.jpg"
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 24), color=(10, 80, 160)).save(img, format="JPEG")

    posts_json = root / "your_instagram_activity" / "media" / "posts_1.json"
    posts_json.parent.mkdir(parents=True, exist_ok=True)
    posts_json.write_text(
        json.dumps(
            [
                {
                    "title": "제주도".encode("utf-8").decode("latin-1"),
                    "creation_timestamp": 1700000000,
                    "media": [{"uri": "media/posts/101.jpg", "creation_timestamp": 1700000000}],
                }
            ]
        ),
        encoding="utf-8",
    )

    followers_json = root / "connections" / "followers_and_following" / "followers_1.json"
    followers_json.parent.mkdir(parents=True, exist_ok=True)
    followers_json.write_text(
        json.dumps([{"string_list_data": [{"value": "alice", "timestamp": 1690000000}]}]),
        encoding="utf-8",
    )


def test_cli_verify_pass_and_fail(tmp_path: Path) -> None:
    clean_root = tmp_path / "ig_clean"
    _make_mini_ig_official(clean_root, include_media_file=True)
    report_json = tmp_path / "report.json"

    res_ok = runner.invoke(
        app,
        ["verify", "--ig-official", str(clean_root), "--report-out", str(report_json)],
    )
    assert res_ok.exit_code == 0
    assert "PASS" in res_ok.stdout
    assert report_json.exists()
    assert json.loads(report_json.read_text(encoding="utf-8"))["passed_gate"] is True

    broken_root = tmp_path / "ig_broken"
    _make_mini_ig_official(broken_root, include_media_file=False)
    res_fail = runner.invoke(app, ["verify", "--ig-official", str(broken_root)])
    assert res_fail.exit_code == 1
    assert "FAIL" in res_fail.stdout
    assert "MISSING_MEDIA" in res_fail.stdout


def test_cli_build_end_to_end(tmp_path: Path) -> None:
    ig_off = tmp_path / "ig_official"
    _make_mini_ig_official(ig_off, include_media_file=True)

    ig_col = tmp_path / "ig_collected"
    (ig_col / "posts").mkdir(parents=True)
    (ig_col / "likes").mkdir(parents=True)
    col_img = ig_col / "media" / "posts" / "SC_MINI_01.jpg"
    col_img.parent.mkdir(parents=True)
    Image.new("RGB", (24, 24), color=(10, 80, 160)).save(col_img, format="JPEG")

    col_post = Post(
        platform="instagram",
        post_id="pw_1",
        shortcode="SC_MINI",
        timestamp_unix=1700000005,
        caption="제주도!",
        media=[MediaItem(media_type="image", local_path="media/instagram/posts/SC_MINI_01.jpg")],
        source="playwright",
    )
    (ig_col / "posts" / "SC_MINI.json").write_text(col_post.model_dump_json(), encoding="utf-8")
    (ig_col / "likes" / "SC_MINI.json").write_text(
        LikesInfo(count=1, users=[UserRef(username="bob")]).model_dump_json(),
        encoding="utf-8",
    )
    (ig_col / "connections.json").write_text(
        json.dumps(
            [
                Connection(
                    platform="instagram",
                    relation="follower",
                    username="alice",
                    full_name="앨리스",
                ).model_dump(mode="json")
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "archive"
    res = runner.invoke(
        app,
        [
            "build",
            "--ig-official",
            str(ig_off),
            "--ig-collected",
            str(ig_col),
            "--output-dir",
            str(out_dir),
        ],
    )
    assert res.exit_code == 0
    assert "아카이브 빌드 완료" in res.stdout

    paths = ArchivePaths(out_dir)
    assert paths.db_path.exists()
    assert paths.manifest_path.exists()
    assert paths.posts_json.exists()
    assert paths.connections_json.exists()

    posts_data = json.loads(paths.posts_json.read_text(encoding="utf-8"))
    assert len(posts_data) == 1
    assert posts_data[0]["shortcode"] == "SC_MINI"
    assert posts_data[0]["likes"]["users"][0]["username"] == "bob"
    xmp_rel = posts_data[0]["media"][0]["xmp_path"]
    assert xmp_rel and (paths.root / xmp_rel).exists()

    conns_data = json.loads(paths.connections_json.read_text(encoding="utf-8"))
    assert conns_data[0]["username"] == "alice"
    assert conns_data[0]["full_name"] == "앨리스"
