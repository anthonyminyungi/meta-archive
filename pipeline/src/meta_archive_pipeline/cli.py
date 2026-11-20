"""CLI entrypoint for Meta Archive desktop pipeline."""

import typer

from meta_archive_pipeline import __version__

app = typer.Typer(
    name="meta-archive",
    help="Instagram & Facebook 개인 아카이브 수집·검증·빌드 CLI",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Manage personal Instagram and Facebook archive pipeline."""


@app.command()
def version() -> None:
    """Print the installed meta-archive-pipeline version."""
    typer.echo(f"meta-archive {__version__}")
