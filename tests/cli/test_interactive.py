"""Comprehensive tests for Support CLI entry point, workspace, compact status presentation, and database management UX."""

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest
from typer.testing import CliRunner

from support.application.errors import (
    AuthenticationFailedError,
    CredentialError,
    DatabaseConnectionError,
    ProfileError,
)
from support.application.ports import ConnectionTestResult
from support.cli.composition import get_profile_service
from support.cli.db.interactive import (
    compact_add_connection,
    interactive_list_connections,
    interactive_remove_connection,
    interactive_test_connection,
    run_db_management,
)
from support.cli.main import app
from support.cli.presentation import (
    DatabaseStatusInfo,
    format_duration,
    format_endpoint,
    format_engine_name,
    get_console,
    print_status,
    render_database_status_line,
    render_model_status_line,
)
from support.cli.workspace import run_workspace
from support.domain.profiles import (
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)

runner = CliRunner()


# -----------------------------------------------------------------------------
# 1. Default CLI behavior & Workspace Entry Point
# -----------------------------------------------------------------------------


def test_main_non_interactive_preserves_welcome_message() -> None:
    """Non-TTY invocation of 'support' must show welcome message without hanging."""
    res = runner.invoke(app, [])
    assert res.exit_code == 0
    assert "Welcome to Support" in res.output
    assert "support --help" in res.output
    assert "support >" not in res.output


def test_main_interactive_triggers_workspace() -> None:
    """TTY invocation of 'support' must invoke run_workspace."""
    with patch("support.cli.main._is_interactive_terminal", return_value=True), patch(
        "support.cli.workspace.run_workspace"
    ) as mock_workspace:
        res = runner.invoke(app, [])
        assert res.exit_code == 0
        mock_workspace.assert_called_once()


def test_main_subcommand_bypasses_workspace(tmp_path: Path) -> None:
    """Explicit subcommands bypass interactive workspace."""
    env = {"SUPPORT_HOME": str(tmp_path)}
    with patch("support.cli.workspace.run_workspace") as mock_workspace:
        res = runner.invoke(app, ["db", "list"], env=env)
        assert res.exit_code == 0
        assert "No profiles found" in res.output
        mock_workspace.assert_not_called()


# -----------------------------------------------------------------------------
# 2. First-Time Setup & Active Profile Selection
# -----------------------------------------------------------------------------


def test_workspace_first_time_setup_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When 0 profiles exist and user declines onboarding, enters workspace with Database: (none) · Not configured."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=["/exit"]
    ):
        mock_confirm.return_value.ask.return_value = False

        run_workspace()

        mock_confirm.assert_called_once_with("Add your first database?", default=True)
        captured = capsys.readouterr().out
        assert "No database connections configured." in captured
        assert "Database:  (none)  ·  Not configured" in captured
        assert "Model:     Not configured" in captured
        assert "Goodbye!" in captured


def test_workspace_first_time_setup_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When 0 profiles exist and user accepts onboarding, guides through compact setup."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()

    def mock_add_flow() -> str:
        service.add_profile("my-first-db", SQLiteConnection(database_path=":memory:"))
        return "my-first-db"

    with patch("questionary.confirm") as mock_confirm, patch(
        "support.cli.workspace.compact_add_connection", side_effect=mock_add_flow
    ) as mock_add, patch(
        "builtins.input", side_effect=["/exit"]
    ):
        mock_confirm.return_value.ask.return_value = True

        run_workspace()

        mock_confirm.assert_called_once_with("Add your first database?", default=True)
        mock_add.assert_called_once()
        captured = capsys.readouterr().out
        assert "Database:  my-first-db (SQLite)  ·  Online" in captured
        assert "Model:     Not configured" in captured
        assert "Goodbye!" in captured


def test_workspace_single_profile_auto_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When 1 profile exists, it is selected automatically and onboarding is skipped."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("auto-db", SQLiteConnection(database_path=":memory:"))

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=["/exit"]
    ):
        run_workspace()

        mock_confirm.assert_not_called()
        captured = capsys.readouterr().out
        assert "Database:  auto-db (SQLite)  ·  Online" in captured
        assert "Model:     Not configured" in captured


def test_workspace_multiple_profiles_prompts_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When multiple profiles exist, user is prompted with compact selector."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("db-alpha", SQLiteConnection(database_path=":memory:"))
    service.add_profile("db-beta", SQLiteConnection(database_path=":memory:"))

    with patch("questionary.select") as mock_select, patch(
        "builtins.input", side_effect=["/exit"]
    ):
        mock_select.return_value.ask.return_value = "db-beta"

        run_workspace()

        mock_select.assert_called_once_with(
            "Select active database connection:",
            choices=["db-alpha", "db-beta"],
        )
        captured = capsys.readouterr().out
        assert "Database:  db-beta (SQLite)  ·  Online" in captured


def test_workspace_unreachable_database_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unreachable database at startup does not crash, displays Offline status, and stays on profile."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile(
        "remote-pg",
        PostgresConnection(host="down.local", database="test", username="user", ssl_mode="prefer"),
    )

    with patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=DatabaseConnectionError("Host unreachable"),
    ), patch("builtins.input", side_effect=["/exit"]):
        run_workspace()

        captured = capsys.readouterr().out
        assert "Database:  remote-pg (PostgreSQL)  ·  Offline" in captured
        assert "Reason: Host unreachable" in captured
        assert "Model:     Not configured" in captured


# -----------------------------------------------------------------------------
# 3. Workspace REPL Commands & Status Command
# -----------------------------------------------------------------------------


def test_workspace_repl_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Workspace handles help, status, clear, empty input, unknown commands, and exit."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("status-db", SQLiteConnection(database_path=":memory:"))

    inputs = ["/help", "", "/status", "/clear", "/unknown_cmd", "/q"]

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=inputs
    ), patch("support.cli.workspace.clear_screen") as mock_clear:
        mock_confirm.return_value.ask.return_value = False

        run_workspace()

        mock_clear.assert_called_once()
        captured = capsys.readouterr().out
        assert "Support — Workspace Guide" in captured
        assert "/status               Show database and model status" in captured
        assert "Database:  status-db (SQLite)  ·  Online" in captured
        assert "Endpoint:  :memory:" in captured
        assert "Unknown command: /unknown_cmd" in captured
        assert "Type /help to see available commands." in captured
        assert "Goodbye!" in captured


def test_workspace_status_no_active_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running '/status' without active profile shows clear guidance."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=["/status", "/exit"]
    ):
        mock_confirm.return_value.ask.return_value = False

        run_workspace()

        captured = capsys.readouterr().out
        assert "No active database connection." in captured
        assert "Use 'support db' to manage database connections." in captured


def test_workspace_repl_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Workspace handles KeyboardInterrupt gracefully."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.confirm") as mock_confirm, patch(
        "builtins.input", side_effect=KeyboardInterrupt
    ):
        mock_confirm.return_value.ask.return_value = False

        run_workspace()

        captured = capsys.readouterr().out
        assert "Goodbye!" in captured


# -----------------------------------------------------------------------------
# 4. Database Management Command ('support db')
# -----------------------------------------------------------------------------


def test_support_db_non_interactive_shows_examples() -> None:
    """Running 'support db' in non-TTY displays practical examples guide."""
    res = runner.invoke(app, ["db"])
    assert res.exit_code == 0
    assert "Manage registered database connections." in res.output
    assert "support db add" in res.output
    assert "support db list" in res.output
    assert "support db test production" in res.output
    assert "support db remove staging" in res.output


def test_support_db_help_flag_shows_examples() -> None:
    """Running 'support db --help' displays practical examples guide."""
    res = runner.invoke(app, ["db", "--help"])
    assert res.exit_code == 0
    assert "support db add" in res.output
    assert "support db list" in res.output
    assert "support db test production" in res.output
    assert "support db remove staging" in res.output


def test_support_db_interactive_triggers_management_menu() -> None:
    """Running 'support db' in TTY invokes run_db_management."""
    with patch("support.cli.db._is_interactive_terminal", return_value=True), patch(
        "support.cli.db.run_db_management"
    ) as mock_mgmt:
        res = runner.invoke(app, ["db"])
        assert res.exit_code == 0
        mock_mgmt.assert_called_once()


def test_support_db_explicit_subcommands_work(tmp_path: Path) -> None:
    """Explicit subcommands execute directly without interactive menus."""
    env = {"SUPPORT_HOME": str(tmp_path)}

    # 1. db add
    res_add = runner.invoke(
        app,
        ["db", "add", "--name", "test-db", "--type", "sqlite", "--database-path", ":memory:"],
        env=env,
    )
    assert res_add.exit_code == 0
    assert "Profile 'test-db' added" in res_add.output

    # 2. db list
    res_list = runner.invoke(app, ["db", "list"], env=env)
    assert res_list.exit_code == 0
    assert "test-db" in res_list.output

    # 3. db test (includes Connect time)
    res_test = runner.invoke(app, ["db", "test", "test-db"], env=env)
    assert res_test.exit_code == 0
    assert "Connection succeeded." in res_test.output
    assert "Connect time:" in res_test.output

    # 4. db remove
    res_rm = runner.invoke(app, ["db", "remove", "test-db", "--yes"], env=env)
    assert res_rm.exit_code == 0
    assert "Profile 'test-db' removed" in res_rm.output


def test_backward_compatibility_support_profile_subcommands(tmp_path: Path) -> None:
    """Legacy 'support profile ...' commands continue to work identically."""
    env = {"SUPPORT_HOME": str(tmp_path)}
    res_add = runner.invoke(
        app,
        ["profile", "add", "--name", "compat-db", "--type", "sqlite", "--database-path", ":memory:"],
        env=env,
    )
    assert res_add.exit_code == 0
    assert "Profile 'compat-db' added" in res_add.output

    res_list = runner.invoke(app, ["profile", "list"], env=env)
    assert res_list.exit_code == 0
    assert "compat-db" in res_list.output


# -----------------------------------------------------------------------------
# 5. Compact Interactive Connection Setup (Test Before Save)
# -----------------------------------------------------------------------------


def test_compact_add_connection_sqlite_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive add connection tests connection before saving and stores profile on success."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.select") as mock_select, patch(
        "questionary.text"
    ) as mock_text, patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=ConnectionTestResult(success=True, database_type="sqlite", database=":memory:"),
    ) as mock_test:
        mock_select.return_value.ask.return_value = "SQLite"
        mock_text.side_effect = [
            MagicMock(ask=MagicMock(return_value="sqlite-mem")),
            MagicMock(ask=MagicMock(return_value=":memory:")),
        ]

        created = compact_add_connection()
        assert created == "sqlite-mem"
        mock_test.assert_called_once()

        service = get_profile_service()
        assert service.get_profile("sqlite-mem") is not None


def test_compact_add_connection_postgres_with_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive add PostgreSQL tests connection, saves password securely, and displays tips."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.select") as mock_select, patch(
        "questionary.text"
    ) as mock_text, patch(
        "questionary.password"
    ) as mock_pw, patch(
        "questionary.confirm"
    ) as mock_confirm, patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        return_value=ConnectionTestResult(
            success=True,
            database_type="postgresql",
            database="analytics",
            server_version="16.1",
        ),
    ) as mock_test:
        # 1. select DB type, 2. select SSL mode
        mock_select.side_effect = [
            MagicMock(ask=MagicMock(return_value="PostgreSQL")),
            MagicMock(ask=MagicMock(return_value="prefer")),
        ]
        # name, host, port, database, username
        mock_text.side_effect = [
            MagicMock(ask=MagicMock(return_value="pg-prod")),
            MagicMock(ask=MagicMock(return_value="localhost")),
            MagicMock(ask=MagicMock(return_value="5432")),
            MagicMock(ask=MagicMock(return_value="analytics")),
            MagicMock(ask=MagicMock(return_value="postgres")),
        ]
        mock_pw.return_value.ask.return_value = "supersecret"
        mock_confirm.return_value.ask.return_value = True

        created = compact_add_connection()
        assert created == "pg-prod"
        mock_test.assert_called_once()

        service = get_profile_service()
        assert service.get_profile("pg-prod") is not None
        assert service.get_profile_password("pg-prod") == "supersecret"


def test_compact_add_connection_test_fails_cancel_leaves_no_orphaned_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When connection test fails and user cancels, NO profile or credentials remain."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.select") as mock_select, patch(
        "questionary.text"
    ) as mock_text, patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=DatabaseConnectionError("Connection refused on port 5432"),
    ):
        mock_select.side_effect = [
            MagicMock(ask=MagicMock(return_value="SQLite")),
            MagicMock(ask=MagicMock(return_value="Cancel")),
        ]
        mock_text.side_effect = [
            MagicMock(ask=MagicMock(return_value="bad-db")),
            MagicMock(ask=MagicMock(return_value="./bad.db")),
        ]

        created = compact_add_connection()
        assert created is None

        # Verify no orphaned profile in SQLite
        service = get_profile_service()
        assert len(service.list_profiles()) == 0


def test_compact_add_connection_test_fails_save_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When connection test fails, user can optionally save connection anyway."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))

    with patch("questionary.select") as mock_select, patch(
        "questionary.text"
    ) as mock_text, patch(
        "support.infrastructure.dispatcher.ConnectorDispatcher.test_connection",
        side_effect=DatabaseConnectionError("Connection refused on port 5432"),
    ):
        mock_select.side_effect = [
            MagicMock(ask=MagicMock(return_value="SQLite")),
            MagicMock(ask=MagicMock(return_value="Save connection anyway")),
        ]
        mock_text.side_effect = [
            MagicMock(ask=MagicMock(return_value="offline-db")),
            MagicMock(ask=MagicMock(return_value="./offline.db")),
        ]

        created = compact_add_connection()
        assert created == "offline-db"

        service = get_profile_service()
        assert service.get_profile("offline-db") is not None


# -----------------------------------------------------------------------------
# 6. Interactive Database Management Menu Actions
# -----------------------------------------------------------------------------


def test_interactive_list_and_remove_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive listing displays formatted list, and interactive remove deletes profile."""
    monkeypatch.setenv("SUPPORT_HOME", str(tmp_path))
    service = get_profile_service()
    service.add_profile("to-remove", SQLiteConnection(database_path=":memory:"))

    with patch("typer.echo") as mock_echo:
        interactive_list_connections()
        messages = [call.args[0] for call in mock_echo.call_args_list if call.args]
        assert any("to-remove" in m for m in messages)

    with patch("questionary.select") as mock_select, patch(
        "questionary.confirm"
    ) as mock_confirm, patch("typer.echo") as mock_echo:
        mock_select.return_value.ask.return_value = "to-remove"
        mock_confirm.return_value.ask.return_value = True

        interactive_remove_connection()
        assert len(service.list_profiles()) == 0
        messages = [call.args[0] for call in mock_echo.call_args_list if call.args]
        assert any("✓ Connection 'to-remove' removed." in m for m in messages)


def test_run_db_management_menu_exit() -> None:
    """Database management menu exits cleanly when user selects Exit."""
    with patch("questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "Exit"
        run_db_management()
        mock_select.assert_called_once()


# -----------------------------------------------------------------------------
# 7. Presentation & Status Formatting Unit Tests
# -----------------------------------------------------------------------------


def test_format_engine_names() -> None:
    """Friendly database engine names format naturally."""
    assert format_engine_name("postgresql") == "PostgreSQL"
    assert format_engine_name("mysql") == "MySQL"
    assert format_engine_name("mariadb") == "MariaDB"
    assert format_engine_name("sqlite") == "SQLite"
    assert format_engine_name("sqlserver") == "SQL Server"
    assert format_engine_name("mongodb") == "MongoDB"
    assert format_engine_name(DatabaseType.POSTGRESQL) == "PostgreSQL"
    assert format_engine_name(DatabaseType.SQLSERVER) == "SQL Server"
    assert format_engine_name(None) == "Unknown"


def test_format_duration() -> None:
    """Duration formatting converts seconds accurately to readable ms and s."""
    assert format_duration(None) == ""
    assert format_duration(0.0004) == "< 1 ms"
    assert format_duration(0.042) == "42 ms"
    assert format_duration(0.113) == "113 ms"
    assert format_duration(1.25) == "1.25 s"


def test_format_endpoints() -> None:
    """Endpoints format correctly for all database types without leaking secrets."""
    pg_conn = PostgresConnection(host="localhost", port=5432, database="test", username="user", ssl_mode="prefer")
    assert format_endpoint(pg_conn) == "localhost:5432"

    sqlite_conn = SQLiteConnection(database_path="/tmp/test.db")
    assert format_endpoint(sqlite_conn) == "/tmp/test.db"

    sqlite_mem = SQLiteConnection(database_path=":memory:")
    assert format_endpoint(sqlite_mem) == ":memory:"

    # MongoDB with URI: extracts host/port without exposing query parameters
    mongo_uri_conn = MongoDBConnection(
        uri="mongodb://cluster0.example.mongodb.net:27017/analytics?ssl=true",
        database="analytics",
    )
    endpoint = format_endpoint(mongo_uri_conn)
    assert endpoint == "cluster0.example.mongodb.net:27017"
    assert "ssl=true" not in endpoint

    mongo_conn = MongoDBConnection(
        uri="mongodb://mongo.internal:27017",
        database="test",
    )
    assert format_endpoint(mongo_conn) == "mongo.internal:27017"

    assert format_endpoint(None) == "none"


def test_render_database_status_line() -> None:
    """Database status line formats online, offline, and not configured states."""
    # Online
    info_online = DatabaseStatusInfo(
        profile_name="pg-prod",
        engine_name="PostgreSQL",
        is_online=True,
        duration_seconds=0.113,
    )
    rendered = render_database_status_line(info_online)
    assert "Database:" in rendered
    assert "pg-prod (PostgreSQL)" in rendered
    assert "Online" in rendered
    assert "113 ms" in rendered

    # Offline
    info_offline = DatabaseStatusInfo(
        profile_name="pg-prod",
        engine_name="PostgreSQL",
        is_online=False,
        failure_reason="Connection refused",
    )
    rendered_off = render_database_status_line(info_offline)
    assert "pg-prod (PostgreSQL)" in rendered_off
    assert "Offline" in rendered_off
    assert "Reason: Connection refused" in rendered_off

    # Not configured
    info_none = DatabaseStatusInfo(profile_name="(none)")
    rendered_none = render_database_status_line(info_none)
    assert "(none)" in rendered_none
    assert "Not configured" in rendered_none


def test_render_model_status_line() -> None:
    """Model status line renders Not configured, Available, and Unavailable."""
    assert "Not configured" in render_model_status_line(None)
    assert "Available" in render_model_status_line("ollama/qwen2.5-coder:7b", is_available=True)
    assert "Unavailable" in render_model_status_line("ollama/qwen2.5-coder:7b", is_available=False)


def test_no_color_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NO_COLOR environment variable is set, Console disables colors."""
    monkeypatch.setenv("NO_COLOR", "1")
    console = get_console()
    assert console.no_color is True
