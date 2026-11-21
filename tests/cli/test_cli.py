"""Test suite for the Support CLI entry point."""

from typer.testing import CliRunner

from support import __version__
from support.cli.main import app

runner = CliRunner()


def test_cli_welcome_message() -> None:
    """Running support without arguments should display the welcome message."""
    result = runner.invoke(app)
    assert result.exit_code == 0
    assert "Welcome to Support" in result.output
    assert f"v{__version__}" in result.output
    assert "support --help" in result.output


def test_cli_version_long_flag() -> None:
    """Running support --version should output the version string."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "support version" in result.output
    assert __version__ in result.output


def test_cli_version_short_flag() -> None:
    """Running support -v or -V should output the version string."""
    for flag in ["-v", "-V"]:
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0
        assert "support version" in result.output
        assert __version__ in result.output


def test_cli_help() -> None:
    """Running support --help should display help instructions and options."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Support — Developer-First Terminal Database Workspace." in result.output
    assert "--version" in result.output
    assert "--help" in result.output
