"""Tests for strict slash-command routing and conversational assistant integration in the Support workspace."""

from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from support.cli.composition import get_profile_service
from support.cli.workspace import run_workspace
from support.domain.assistant import ChatMessage, ModelInfo, ToolCall
from support.domain.profiles import SQLiteConnection


def _setup_test_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL);")
    conn.close()


def test_ordinary_keywords_routed_to_ollama_not_intercepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Words like 'tables', 'schema', 'describe users', 'help', 'exit' WITHOUT leading '/' MUST reach Ollama."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    db_path = tmp_path / "test.db"
    _setup_test_db(db_path)
    service = get_profile_service()
    service.add_profile("test-db", SQLiteConnection(database_path=str(db_path)))

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )
    # The assistant receives natural language messages
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", content="Assistant answering 'tables' prompt"),
        ChatMessage(role="assistant", content="Assistant answering 'help' prompt"),
        ChatMessage(role="assistant", content="Assistant answering 'exit' prompt"),
    ]

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input",
        side_effect=[
            "tables",
            "help",
            "exit",
            "/exit",  # actual slash exit
        ],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()

        captured = capsys.readouterr().out
        assert "Assistant answering 'tables' prompt" in captured
        assert "Assistant answering 'help' prompt" in captured
        assert "Assistant answering 'exit' prompt" in captured
        assert "Goodbye!" in captured


def test_unknown_slash_command_rejected_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown slash command is rejected locally and NEVER sent to Ollama."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input",
        side_effect=[
            "/unknown",
            "/custom_command",
            "/q",
        ],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()

        # LLM provider chat should NEVER have been called for slash commands
        mock_llm.chat.assert_not_called()

        captured = capsys.readouterr().out
        assert "Unknown command: /unknown" in captured
        assert "Unknown command: /custom_command" in captured
        assert "Type /help to see available commands." in captured
        assert "Goodbye!" in captured


def test_url_with_slashes_routed_to_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Messages containing slashes that do not start with '/' are routed to the assistant."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )
    mock_llm.chat.return_value = ChatMessage(role="assistant", content="That URL looks like an API endpoint.")

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input",
        side_effect=[
            "What does http://localhost:8000/api/users do?",
            "/exit",
        ],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()

        mock_llm.chat.assert_called_once()
        captured = capsys.readouterr().out
        assert "That URL looks like an API endpoint." in captured
        assert "Goodbye!" in captured


def test_slash_help_guide_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify exact slash help guide contents."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=["/help", "/exit"]
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()

        captured = capsys.readouterr().out
        assert "Support — Workspace Guide" in captured
        assert "Ask naturally:" in captured
        assert "Show all tables in this database" in captured
        assert "Slash Commands:" in captured
        assert "/help                 Show this guide" in captured
        assert "/status               Show database and model status" in captured
        assert "/clear                Clear screen and conversation history" in captured
        assert "/tables               List database objects" in captured
        assert "/schema               Show database structure" in captured
        assert "/describe <object>    Inspect an object" in captured
        assert "/relationships        Show declared relationships" in captured
        assert "/exit                 Exit Support" in captured
        assert "Model configuration:" in captured
        assert "support model" in captured
        assert "Database management:" in captured
        assert "support db" in captured
        assert "All input without a leading '/' is sent to the AI assistant." in captured
