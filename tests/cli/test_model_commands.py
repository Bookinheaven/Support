"""Tests for Support CLI model management commands (show, list, set, unset, test)."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from support.application.errors import OllamaConnectionError
from support.cli.main import app
from support.domain.assistant import ChatMessage, ModelInfo, ToolCall
from support.infrastructure.config import get_assistant_model, load_config

runner = CliRunner()


def test_model_show_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    res = runner.invoke(app, ["model", "show"])
    assert res.exit_code == 0
    assert "Assistant Model:    (none)" in res.output
    assert "Status:             Not configured" in res.output
    assert "support model set assistant <model-name>" in res.output


def test_model_show_configured_and_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:7b")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:7b",
        is_available=True,
        supports_tools=True,
        details="Available",
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "show"])
        assert res.exit_code == 0
        assert "Assistant Model:    qwen2.5-coder:7b" in res.output
        assert "Status:             Available" in res.output
        assert "Tool Calling:       Supported" in res.output


def test_model_show_configured_but_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "missing-model:latest")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="missing-model:latest",
        is_available=False,
        supports_tools=False,
        details="Model not found on Ollama server",
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "show"])
        assert res.exit_code == 0
        assert "Assistant Model:    missing-model:latest" in res.output
        assert "Status:             Model not found on Ollama server" in res.output
        assert "Tool Calling:       Unavailable" in res.output


def test_model_list_success() -> None:
    mock_llm = MagicMock()
    mock_llm.list_models.return_value = [
        ModelInfo(name="qwen2.5-coder:3b", is_available=True),
        ModelInfo(name="qwen2.5-coder:7b", is_available=True),
    ]

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "list"])
        assert res.exit_code == 0
        assert "Installed Ollama Models:" in res.output
        assert "qwen2.5-coder:3b" in res.output
        assert "qwen2.5-coder:7b" in res.output


def test_model_list_empty() -> None:
    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.list_models.return_value = []

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "list"])
        assert res.exit_code == 0
        assert "No models found on Ollama server" in res.output
        assert "ollama pull" in res.output


def test_model_list_connection_error() -> None:
    mock_llm = MagicMock()
    mock_llm.list_models.side_effect = OllamaConnectionError("Failed to connect to http://localhost:11434")

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "list"])
        assert res.exit_code == 1
        assert "Failed to connect to http://localhost:11434" in res.output


def test_model_set_unsupported_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    res = runner.invoke(app, ["model", "set", "critic", "qwen2.5-coder:7b"])
    assert res.exit_code == 1
    assert "Unsupported role 'critic'" in res.output


def test_model_set_unavailable_model_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="nonexistent:latest",
        is_available=False,
        details="Model not found",
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "set", "assistant", "nonexistent:latest"])
        assert res.exit_code == 1
        assert "is not available on Ollama server" in res.output
        assert "ollama pull nonexistent:latest" in res.output


def test_model_set_success_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "set", "assistant", "qwen2.5-coder:3b"])
        assert res.exit_code == 0
        assert "Previous assistant model: (none)" in res.output
        assert "Configured assistant model: qwen2.5-coder:3b" in res.output
        assert "Configuration saved to" in res.output

        # Verify persisted configuration
        assert get_assistant_model() == "qwen2.5-coder:3b"


def test_model_unset_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    cfg = load_config()
    cfg.set_model("assistant", "qwen2.5-coder:3b")
    from support.infrastructure.config import save_config
    save_config(cfg)
    assert get_assistant_model() == "qwen2.5-coder:3b"

    res = runner.invoke(app, ["model", "unset", "assistant"])
    assert res.exit_code == 0
    assert "Removed assistant model 'qwen2.5-coder:3b'" in res.output
    assert get_assistant_model() is None


def test_model_unset_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    res = runner.invoke(app, ["model", "unset", "assistant"])
    assert res.exit_code == 0
    assert "No model was configured for role 'assistant'" in res.output


def test_model_test_no_model_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.delenv("SUPPORT_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    res = runner.invoke(app, ["model", "test"])
    assert res.exit_code == 1
    assert "Error: No assistant model configured." in res.output


def test_model_test_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )
    tc = ToolCall(id="c1", name="test_tool", arguments={"val": "ok"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "test"])
        assert res.exit_code == 0
        assert "Server:       http://localhost:11434 (Online)" in res.output
        assert "Model:        qwen2.5-coder:3b (Available)" in res.output
        assert "Tool Calling: Verified" in res.output
        assert "Result:       Success" in res.output


def test_model_test_failure_model_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=False,
        details="Connection refused",
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm):
        res = runner.invoke(app, ["model", "test"])
        assert res.exit_code == 1
        assert "Result:       Failed (Connection refused)" in res.output


def test_model_test_eval_suite_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm), patch(
        "support.cli.model.run_model_behavioral_eval", return_value=True
    ) as mock_eval:
        res = runner.invoke(app, ["model", "test", "--eval"])
        assert res.exit_code == 0
        mock_eval.assert_called_once()


def test_model_test_eval_suite_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.base_url = "http://localhost:11434"
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    with patch("support.cli.model.get_llm_provider", return_value=mock_llm), patch(
        "support.cli.model.run_model_behavioral_eval", return_value=False
    ) as mock_eval:
        res = runner.invoke(app, ["model", "test", "--eval"])
        assert res.exit_code == 1
        mock_eval.assert_called_once()


def test_model_interactive_menu_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("support.cli.model._is_interactive_terminal", return_value=True), patch(
        "questionary.select"
    ) as mock_select:
        mock_select.return_value.ask.return_value = "Exit"
        res = runner.invoke(app, ["model"])
        assert res.exit_code == 0
        mock_select.assert_called_once()
