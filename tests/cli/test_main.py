"""CLI tests for Support main entry point."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from support import __version__
from support.cli.main import app, cli

runner = CliRunner()


def test_cli_default_invocation() -> None:
    """Running support without arguments should display welcome message and exit with 0."""
    result = runner.invoke(app)
    assert result.exit_code == 0
    assert "Welcome to Support" in result.output
    assert f"v{__version__}" in result.output
    assert "support --help" in result.output


def test_cli_help() -> None:
    """Running support --help should display usage information and exit with 0."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Support" in result.output
    assert "--help" in result.output
    assert "--version" in result.output
    assert "-V" in result.output
    # -v should not be listed as version option
    assert "-v," not in result.output


def test_cli_version_long_flag() -> None:
    """Running support --version should output the version and exit with 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"support version {__version__}" in result.output


def test_cli_version_short_flag() -> None:
    """Running support -V should output the version and exit with 0."""
    result = runner.invoke(app, ["-V"])
    assert result.exit_code == 0
    assert f"support version {__version__}" in result.output


def test_cli_version_lowercase_v_rejected() -> None:
    """Running support -v must be rejected as an unknown option, reserved for future verbose mode."""
    result = runner.invoke(app, ["-v"])
    assert result.exit_code == 2
    assert "No such option: -v" in result.output


def test_cli_unknown_command() -> None:
    """Running support with an unknown subcommand should fail with exit code 2 and helpful error."""
    result = runner.invoke(app, ["nonexistent-command"])
    assert result.exit_code == 2
    assert "No such command" in result.output
    assert "nonexistent-command" in result.output


def test_cli_unknown_option() -> None:
    """Running support with an unknown option should fail with exit code 2 and helpful error."""
    result = runner.invoke(app, ["--invalid-option"])
    assert result.exit_code == 2
    assert "No such option: --invalid-option" in result.output


def test_cli_keyboard_interrupt(capsys: pytest.CaptureFixture[str]) -> None:
    """Simulated KeyboardInterrupt in cli() wrapper exits with code 130 and leaks no traceback."""
    with patch("support.cli.main.app", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            cli()
        assert exc_info.value.code == 130
        captured = capsys.readouterr()
        assert "Aborted." in captured.out
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err
