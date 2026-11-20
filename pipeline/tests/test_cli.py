"""Tests for the meta-archive CLI entrypoint."""

from typer.testing import CliRunner

from meta_archive_pipeline import __version__
from meta_archive_pipeline.cli import app

runner = CliRunner()


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout
    assert "Instagram & Facebook" in result.stdout


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"meta-archive {__version__}" in result.stdout
