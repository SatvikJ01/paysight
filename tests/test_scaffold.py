"""Smoke tests for the M0 scaffold: settings load and the CLI responds."""

from datetime import date

from typer.testing import CliRunner

from paysight import __version__
from paysight.cli import app
from paysight.config import get_settings

runner = CliRunner()


def test_settings_load_with_fixed_as_of_date() -> None:
    settings = get_settings()
    assert settings.data.as_of_date == date(2026, 10, 31)
    assert settings.data.start_date == date(2026, 8, 1)
    assert settings.data.seed == 42
    assert settings.duckdb_path.is_absolute()


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "paysight" in result.output.lower()


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
