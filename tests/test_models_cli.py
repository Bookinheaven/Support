"""Tests for LLM models CLI commands (support models)."""

from pathlib import Path
import pytest
from typer.testing import CliRunner
from support.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_isolated_env(tmp_path: Path, monkeypatch):
    """Isolated environment pointing SUPPORT_CONFIG_DIR to tmp_path."""
    config_dir = tmp_path / ".support"
    monkeypatch.setenv("SUPPORT_CONFIG_DIR", str(config_dir))
    # Clear cloud keys for consistent tests
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return config_dir


def test_cli_models_list_and_current(cli_isolated_env):
    # support models (invoked without subcommands)
    res = runner.invoke(app, ["models"])
    assert res.exit_code == 0
    assert "Provider" in res.stdout
    assert "Ollama" in res.stdout
    assert "Grok" in res.stdout
    assert "Gemini" in res.stdout

    # support models current
    cur_res = runner.invoke(app, ["models", "current"])
    assert cur_res.exit_code == 0
    assert "Active Provider: ollama" in cur_res.stdout
    assert "Active Model:    qwen2.5-coder:7b" in cur_res.stdout


def test_cli_models_use_and_switch(cli_isolated_env):
    # Switch to grok (unconfigured warning, but sets config)
    use_res = runner.invoke(app, ["models", "use", "grok", "--model", "grok-2-latest"])
    assert use_res.exit_code == 0
    assert "Active model set to 'grok-2-latest' (grok)" in use_res.stdout

    # Verify current reflects grok
    cur_res = runner.invoke(app, ["models", "current"])
    assert cur_res.exit_code == 0
    assert "Active Provider: grok" in cur_res.stdout
    assert "Active Model:    grok-2-latest" in cur_res.stdout

    # Switch back to ollama
    use_ollama = runner.invoke(app, ["models", "use", "ollama", "--model", "qwen2.5-coder:7b"])
    assert use_ollama.exit_code == 0
    assert "Active model set to 'qwen2.5-coder:7b' (ollama)" in use_ollama.stdout


def test_cli_models_use_invalid_provider(cli_isolated_env):
    res = runner.invoke(app, ["models", "use", "invalid_provider"])
    assert res.exit_code != 0
    assert "Unsupported LLM provider" in res.stdout


def test_cli_models_test_missing_cloud_key(cli_isolated_env):
    # Testing grok without key fails safely with clean hint
    res = runner.invoke(app, ["models", "test", "grok"])
    assert res.exit_code != 0
    assert "not configured" in res.stdout
    assert "XAI_API_KEY" in res.stdout

    # Testing gemini without key fails safely with clean hint
    res_gem = runner.invoke(app, ["models", "test", "gemini"])
    assert res_gem.exit_code != 0
    assert "not configured" in res_gem.stdout
    assert "GEMINI_API_KEY" in res_gem.stdout
