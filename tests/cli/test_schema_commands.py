"""Tests for workspace REPL schema introspection commands (tables, schema, describe, relationships)."""

import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

from support.cli.composition import get_profile_service
from support.cli.workspace import run_workspace
from support.domain.profiles import SQLiteConnection


def _setup_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);")
        conn.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            );
            """
        )
        conn.execute("CREATE VIEW active_users AS SELECT * FROM users;")
    conn.close()


def test_schema_commands_without_active_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    # Reject adding first profile, then issue commands
    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input",
        side_effect=["/tables", "/schema", "/describe users", "/relationships", "/exit"],
    ):
        mock_confirm.return_value.ask.return_value = False
        run_workspace()

        captured = capsys.readouterr().out
        assert captured.count("No active database connection.") == 4
        assert "Goodbye!" in captured


def test_schema_commands_with_active_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    db_file = tmp_path / "app.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=str(db_file)))

    with patch(
        "builtins.input",
        side_effect=[
            "/tables",
            "/schema",
            "/describe users",
            "/describe orders",
            "/relationships",
            "/exit",
        ],
    ):
        run_workspace()

        captured = capsys.readouterr().out
        # tables
        assert "Tables in app.db:" in captured
        assert "users" in captured
        assert "orders" in captured
        assert "Views in app.db:" in captured
        assert "active_users" in captured
        assert "2 tables · 1 view" in captured

        # schema
        assert "Database:      app.db" in captured
        assert "Engine:        SQLite" in captured
        assert "Tables:        2" in captured
        assert "Views:         1" in captured
        assert "Relationships: 1" in captured

        # describe users
        assert "Table:       users" in captured
        assert "id" in captured
        assert "name" in captured
        assert "Primary Key:" in captured
        assert "(id)" in captured

        # describe orders
        assert "Table:       orders" in captured
        assert "orders.user_id -> users.id ON DELETE CASCADE" in captured

        # relationships
        assert "Foreign-key relationships in app.db:" in captured
        assert "orders.user_id" in captured
        assert "-> users.id (ON DELETE CASCADE)" in captured
        assert "1 relationship" in captured


def test_describe_missing_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    db_file = tmp_path / "app.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=str(db_file)))

    with patch("builtins.input", side_effect=["/describe", "/exit"]):
        run_workspace()

        captured = capsys.readouterr().out
        assert "Usage: /describe <object-name>" in captured
        assert "Example: /describe users" in captured


def test_describe_nonexistent_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    db_file = tmp_path / "app.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=str(db_file)))

    with patch("builtins.input", side_effect=["/describe ghost_table", "/exit"]):
        run_workspace()

        captured = capsys.readouterr().out
        assert "Error: Object 'ghost_table' not found in SQLite database." in captured


def test_workspace_help_includes_schema_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    db_file = tmp_path / "app.db"
    _setup_sqlite_db(db_file)

    service = get_profile_service()
    service.add_profile("test-profile", SQLiteConnection(database_path=str(db_file)))

    with patch("builtins.input", side_effect=["/help", "/exit"]):
        run_workspace()

        captured = capsys.readouterr().out
        assert "/tables               List database objects" in captured
        assert "/schema               Show database structure" in captured
        assert "/describe <object>    Inspect an object" in captured
        assert "/relationships        Show declared relationships" in captured
