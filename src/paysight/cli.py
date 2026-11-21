"""Command-line entry point: ``paysight``.

Every project task is exposed here so it runs identically on Linux and Windows.
"""

from __future__ import annotations

import typer

from paysight import __version__
from paysight.config import get_settings

app = typer.Typer(
    name="paysight",
    help="Paysight: an AI payments analyst that shows its work.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the Paysight version."""
    typer.echo(__version__)


@app.command("config")
def show_config() -> None:
    """Print the resolved settings (paths, dates, limits and thresholds)."""
    settings = get_settings()
    typer.echo(settings.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
