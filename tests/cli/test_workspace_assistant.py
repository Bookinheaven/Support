"""Integration tests for Support workspace conversational assistant flow."""

from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch
import pytest

from support.cli.composition import get_profile_service
from support.cli.workspace import run_workspace
from support.domain.assistant import ChatMessage, ModelInfo, ToolCall
from support.domain.profiles import SQLiteConnection


def _setup_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT, price REAL);")
        conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, product_id INTEGER, rating INT);")
    conn.close()


def test_workspace_displays_configured_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:7b")

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:7b",
        is_available=True,
        supports_tools=True,
        details="Available",
    )

    with patch("support.cli.workspace.get_llm_provider", return_value=mock_llm), patch(
        "builtins.input", side_effect=["/exit"]
    ):
        run_workspace()
        captured = capsys.readouterr().out
        assert "Model:     qwen2.5-coder:7b  ·  Available" in captured


def test_workspace_direct_retrieval_renders_once_without_duplicate_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    db_file = tmp_path / "shop.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("shop-profile", SQLiteConnection(database_path=str(db_file)))

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    # Model generates tool call for list_database_objects with response_mode='display'
    tc = ToolCall(id="c1", name="list_database_objects", arguments={"object_type": "all", "response_mode": "display"})
    mock_llm.chat.return_value = ChatMessage(role="assistant", tool_calls=(tc,))

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input",
        side_effect=[
            "Show me what tables exist in this database",
            "/exit",
        ],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()
        captured = capsys.readouterr().out

        # Verify Rich rendered table list is displayed exactly once
        assert "Tables in shop.db:" in captured
        assert "products" in captured
        assert "reviews" in captured

        # Verify NO second model call was made
        assert mock_llm.chat.call_count == 1


def test_workspace_explanatory_query_renders_explanation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    db_file = tmp_path / "shop.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("shop-profile", SQLiteConnection(database_path=str(db_file)))

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=True,
        supports_tools=True,
    )

    tc = ToolCall(id="c1", name="describe_object", arguments={"object_name": "products", "response_mode": "explain"})
    mock_llm.chat.side_effect = [
        ChatMessage(role="assistant", tool_calls=(tc,)),
        ChatMessage(role="assistant", content="The products table stores product records with title and price."),
    ]

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input",
        side_effect=[
            "Explain the purpose of the products table",
            "/exit",
        ],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()
        captured = capsys.readouterr().out

        # Explanation is displayed once
        assert "The products table stores product records with title and price." in captured
        assert mock_llm.chat.call_count == 2


def test_workspace_offline_model_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    monkeypatch.setenv("SUPPORT_MODEL", "qwen2.5-coder:3b")

    mock_llm = MagicMock()
    mock_llm.check_model_status.return_value = ModelInfo(
        name="qwen2.5-coder:3b",
        is_available=False,
        supports_tools=False,
        details="Ollama server offline",
    )

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.get_llm_provider", return_value=mock_llm
    ), patch(
        "builtins.input", side_effect=["/exit"]
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()
        captured = capsys.readouterr().out
        assert "Model:     qwen2.5-coder:3b  ·  Ollama server offline" in captured
