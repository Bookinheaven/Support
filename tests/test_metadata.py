"""Unit tests for centralized application metadata."""

from support.core.metadata import (
    APP_NAME,
    APP_VERSION,
    APP_DESCRIPTION,
    APP_CLI_HELP,
)
from support.cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def test_metadata_constants():
    """Verify application metadata constants are populated and valid."""
    assert APP_NAME == "Support"
    assert isinstance(APP_VERSION, str)
    assert len(APP_VERSION) > 0
    assert "multi-database" in APP_DESCRIPTION.lower()
    assert "Interactive Workspace" in APP_CLI_HELP


def test_cli_version_flag_uses_central_metadata():
    """Verify --version flag outputs centralized version and application name."""
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert APP_NAME in res.stdout
    assert APP_VERSION in res.stdout


def test_cli_main_help_uses_central_metadata():
    """Verify root help text displays the centralized CLI description."""
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "Multi-Database Schema Discovery" in res.stdout

