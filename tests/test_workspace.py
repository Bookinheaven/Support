"""Tests for Interactive Terminal Workspace."""

from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from support.cli.main import app
from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.health import HealthCheckResult
from support.db.service import DatabaseService
from support.llm.models import ModelInfo, ProviderStatus, ProviderValidationResult
from support.llm.service import LLMService
from support.workspace.commands import (
    execute_slash_command,
    handle_clear,
    handle_db_selection,
    handle_help,
    handle_model_selection,
)
from support.workspace.display import (
    display_natural_language_placeholder,
    display_workspace_header,
    display_workspace_help,
)
from support.workspace.session import run_workspace_session
from support.workspace.state import WorkspaceSessionState

runner = CliRunner()


def test_workspace_state_initialization_with_profiles():
    """Verify WorkspaceSessionState initializes active DB and LLM model from services."""
    mock_db = MagicMock(spec=DatabaseService)
    p1 = ConnectionProfile(name="test_pg", dialect=Dialect.POSTGRES, database="test_db")
    h1 = HealthCheckResult(status="ONLINE", latency_ms=15.0)
    mock_db.list_profiles_with_status.return_value = ([p1], {"test_pg": h1})

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5-coder:7b"
    mock_llm.get_config.return_value = mock_cfg
    mock_llm.validate_provider.return_value = ProviderValidationResult(
        provider="ollama",
        status=ProviderStatus.AVAILABLE,
        message="Ollama operational",
        latency_ms=25.0,
    )

    state = WorkspaceSessionState.initialize(db_service=mock_db, llm_service=mock_llm, check_connectivity=True)
    assert state.active_database == "test_pg"
    assert state.database_profile == p1
    assert "ONLINE" in (state.database_status or "")
    assert state.active_provider == "ollama"
    assert state.active_model == "qwen2.5-coder:7b"
    assert "AVAILABLE" in (state.provider_status or "")


def test_workspace_state_initialization_no_profiles():
    """Verify WorkspaceSessionState handles empty profile list gracefully."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([], {})

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5:0.5b"
    mock_llm.get_config.return_value = mock_cfg
    mock_llm.validate_provider.return_value = ProviderValidationResult(
        provider="ollama",
        status=ProviderStatus.AVAILABLE,
        message="Ollama operational",
    )

    state = WorkspaceSessionState.initialize(db_service=mock_db, llm_service=mock_llm)
    assert state.active_database is None
    assert state.database_profile is None
    assert "No databases configured" in (state.database_status or "")


def test_workspace_entrypoint_with_exit():
    """Verify running support without arguments launches workspace and exits on /exit."""
    res = runner.invoke(app, input="/exit\n")
    assert res.exit_code == 0
    assert "Support Interactive Workspace" in res.stdout
    assert "Exiting Support workspace." in res.stdout


def test_workspace_subcommands_remain_intact():
    """Verify existing subcommands still work without launching workspace."""
    # --version
    res_ver = runner.invoke(app, ["--version"])
    assert res_ver.exit_code == 0
    assert "Support version" in res_ver.stdout
    assert "Interactive Workspace" not in res_ver.stdout

    # models current
    res_models = runner.invoke(app, ["models", "current"])
    assert res_models.exit_code == 0
    assert "Active Provider:" in res_models.stdout
    assert "Interactive Workspace" not in res_models.stdout

    # auth status
    res_auth = runner.invoke(app, ["auth", "status"])
    assert res_auth.exit_code == 0
    assert "Grok (xAI)" in res_auth.stdout
    assert "Interactive Workspace" not in res_auth.stdout


def test_workspace_help_command():
    """Verify /help prints available commands overview."""
    res = runner.invoke(app, input="/help\n/exit\n")
    assert res.exit_code == 0
    assert "Workspace Slash Commands" in res.stdout
    assert "/db" in res.stdout
    assert "/model" in res.stdout
    assert "/clear" in res.stdout


def test_workspace_clear_command():
    """Verify /clear clears console and renders title banner without duplicating status lines."""
    state = WorkspaceSessionState(
        active_database="test_db",
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
        database_status="Online · 82 ms",
        provider_status="Available",
    )
    with patch("support.workspace.general_commands.console.clear") as mock_clear, \
         patch("support.workspace.general_commands.display_workspace_title") as mock_title, \
         patch("support.workspace.general_commands.display_workspace_header") as mock_header:
        handle_clear(state)
        mock_clear.assert_called_once()
        mock_title.assert_called_once()
        mock_header.assert_not_called()


def test_workspace_unknown_command():
    """Verify unknown slash commands display an error without crashing."""
    res = runner.invoke(app, input="/invalid_xyz\n/exit\n")
    assert res.exit_code == 0
    assert "Unknown command: '/invalid_xyz'" in res.stdout
    assert "Type /help for available commands" in res.stdout


def test_workspace_natural_language_placeholder():
    """Verify natural-language text prints placeholder and updates history."""
    state = WorkspaceSessionState(
        active_database="sample",
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    inputs = ["List all active users", "/exit"]

    def mock_input(prompt):
        return inputs.pop(0)

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([], {})
    mock_llm = MagicMock(spec=LLMService)

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=mock_input)

    assert len(state.history) == 2
    assert state.history[0].role == "user"
    assert state.history[0].content == "List all active users"
    assert state.history[1].role == "assistant"


def test_workspace_db_selection_success():
    """Verify /db allows switching active database in session."""
    p1 = ConnectionProfile(name="pg_one", dialect=Dialect.POSTGRES, database="db1")
    p2 = ConnectionProfile(name="sqlite_two", dialect=Dialect.SQLITE, database="data.db")
    h1 = HealthCheckResult(status="ONLINE", latency_ms=10.0)
    h2 = HealthCheckResult(status="ONLINE", latency_ms=2.0)

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([p1, p2], {"pg_one": h1, "sqlite_two": h2})

    state = WorkspaceSessionState(active_database="pg_one", database_profile=p1)
    # Select option 2 (sqlite_two)
    handle_db_selection(state, mock_db, user_input_func=lambda p: "2")

    assert state.active_database == "sqlite_two"
    assert state.database_profile == p2


def test_workspace_db_selection_empty_profiles():
    """Verify /db handles empty database list gracefully when user declines guided setup."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([], {})

    state = WorkspaceSessionState(active_database=None)
    # User declines guided setup ("n")
    handle_db_selection(state, mock_db, user_input_func=lambda p: "n")

    assert state.active_database is None


def test_workspace_fast_startup_does_not_probe_network():
    """Verify default WorkspaceSessionState.initialize() loads metadata locally without network calls."""
    mock_db = MagicMock(spec=DatabaseService)
    p = ConnectionProfile(name="fast_pg", dialect=Dialect.POSTGRES, database="db")
    mock_db.list_profiles.return_value = [p]

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5-coder:7b"
    mock_llm.get_config.return_value = mock_cfg

    state = WorkspaceSessionState.initialize(
        db_service=mock_db,
        llm_service=mock_llm,
        check_connectivity=False,
    )

    # Verify no synchronous live network checks were executed
    mock_db.list_profiles_with_status.assert_not_called()
    mock_llm.validate_provider.assert_not_called()

    assert state.active_database == "fast_pg"
    assert state.database_status == "Checking..."
    assert state.active_provider == "ollama"
    assert state.provider_status == "Checking..."


def test_guided_add_database_profile_url_workflow():
    """Verify guided database setup with connection URL."""
    from support.workspace.commands import guided_add_database_profile
    from support.db.health import HealthCheckResult

    mock_db = MagicMock(spec=DatabaseService)
    saved_profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    health = HealthCheckResult(status="ONLINE", latency_ms=12.0, server_version="15.2")
    mock_db.register_profile.return_value = (saved_profile, health)

    state = WorkspaceSessionState()
    inputs = [
        "1",  # Method 1: Connection URL
        "postgresql://user:pass@localhost:5432/testdb",  # URL
        "pg_test",  # Profile name
    ]

    success = guided_add_database_profile(
        mock_db,
        user_input_func=lambda p: inputs.pop(0),
        state=state,
    )

    assert success is True
    assert state.active_database == "pg_test"
    assert "ONLINE" in state.database_status


def test_guided_add_database_profile_unverified_fallback():
    """Verify guided database setup allows saving unverified profile when connection fails."""
    from support.workspace.commands import guided_add_database_profile
    from support.db.service import ConnectionVerificationError
    from support.db.health import HealthCheckResult

    mock_db = MagicMock(spec=DatabaseService)
    fail_health = HealthCheckResult(status="CONNECTION_FAILED", hint="Check host")
    mock_db.register_profile.side_effect = [
        ConnectionVerificationError("pg_failed", fail_health),  # First attempt with test_connection=True
        (ConnectionProfile(name="pg_failed", dialect=Dialect.POSTGRES, database="testdb"), None),  # Second attempt with test_connection=False
    ]

    state = WorkspaceSessionState()
    inputs = [
        "1",  # Method: URL
        "postgresql://user:pass@remote:5432/testdb",
        "pg_failed",
        "y",  # Save unverified anyway? -> YES
    ]

    success = guided_add_database_profile(
        mock_db,
        user_input_func=lambda p: inputs.pop(0),
        state=state,
    )

    assert success is True
    assert state.active_database == "pg_failed"
    assert "Configured (untested)" in state.database_status


def test_workspace_model_selection_success():
    """Verify /model allows switching active provider and model in session without mutating config file."""
    mock_llm = MagicMock(spec=LLMService)
    m1 = ModelInfo(id="qwen2.5:0.5b", name="qwen", provider="ollama")
    m2 = ModelInfo(id="gemini-2.0-flash", name="gemini", provider="gemini")
    mock_llm.list_models.return_value = {"ollama": [m1], "gemini": [m2]}
    mock_llm.validate_all.return_value = {
        "ollama": ProviderValidationResult(provider="ollama", status=ProviderStatus.AVAILABLE, message="Operational"),
        "gemini": ProviderValidationResult(provider="gemini", status=ProviderStatus.NOT_CONFIGURED, message="Key missing"),
    }
    mock_llm.validate_provider.return_value = ProviderValidationResult(
        provider="gemini",
        status=ProviderStatus.NOT_CONFIGURED,
        message="Key missing",
        hint="Set GEMINI_API_KEY",
    )

    state = WorkspaceSessionState(active_provider="ollama", active_model="qwen2.5:0.5b")
    # Select option 2 (gemini-2.0-flash)
    handle_model_selection(state, mock_llm, user_input_func=lambda p: "2")

    assert state.active_provider == "gemini"
    assert state.active_model == "gemini-2.0-flash"
    # Ensure persistent config method was never called
    mock_llm.set_active_provider.assert_not_called()


def test_workspace_keyboard_interrupt_clean_exit():
    """Verify KeyboardInterrupt (Ctrl+C) exits workspace cleanly without traceback."""
    def raise_interrupt(prompt):
        raise KeyboardInterrupt()

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([], {})
    mock_llm = MagicMock(spec=LLMService)

    # Should catch KeyboardInterrupt and terminate loop cleanly
    run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=raise_interrupt)


def test_workspace_eof_clean_exit():
    """Verify EOFError (Ctrl+D) exits workspace cleanly without traceback."""
    def raise_eof(prompt):
        raise EOFError()

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([], {})
    mock_llm = MagicMock(spec=LLMService)

    # Should catch EOFError and terminate loop cleanly
    run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=raise_eof)


def test_workspace_initialization_respects_persisted_default_profile():
    """Verify WorkspaceSessionState.initialize() restores the persisted default profile."""
    p1 = ConnectionProfile(name="athena", dialect=Dialect.POSTGRES, database="db1")
    p2 = ConnectionProfile(name="mydb", dialect=Dialect.POSTGRES, database="db2")
    p3 = ConnectionProfile(name="zeus", dialect=Dialect.POSTGRES, database="db3")

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles.return_value = [p1, p2, p3]
    mock_db.get_default_profile.return_value = "mydb"

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5-coder:7b"
    mock_llm.get_config.return_value = mock_cfg

    state = WorkspaceSessionState.initialize(
        db_service=mock_db,
        llm_service=mock_llm,
        check_connectivity=False,
    )

    assert state.active_database == "mydb"
    assert state.database_profile == p2


def test_workspace_db_selection_persists_default_profile():
    """Verify /db persists chosen profile as default."""
    p1 = ConnectionProfile(name="pg_one", dialect=Dialect.POSTGRES, database="db1")
    p2 = ConnectionProfile(name="sqlite_two", dialect=Dialect.SQLITE, database="data.db")
    h1 = HealthCheckResult(status="ONLINE", latency_ms=10.0)
    h2 = HealthCheckResult(status="ONLINE", latency_ms=2.0)

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles_with_status.return_value = ([p1, p2], {"pg_one": h1, "sqlite_two": h2})

    state = WorkspaceSessionState(active_database="pg_one", database_profile=p1)
    handle_db_selection(state, mock_db, user_input_func=lambda p: "2")

    assert state.active_database == "sqlite_two"
    mock_db.set_default_profile.assert_called_once_with("sqlite_two")


def test_workspace_db_direct_argument_switch():
    """Verify /db <name> switches active database directly and persists it."""
    p1 = ConnectionProfile(name="pg_one", dialect=Dialect.POSTGRES, database="db1")
    p2 = ConnectionProfile(name="sqlite_two", dialect=Dialect.SQLITE, database="data.db")

    mock_db = MagicMock(spec=DatabaseService)
    mock_db.list_profiles.return_value = [p1, p2]

    state = WorkspaceSessionState(active_database="pg_one", database_profile=p1)
    handle_db_selection(state, mock_db, user_input_func=lambda p: "", arg="sqlite_two")

    assert state.active_database == "sqlite_two"
    assert state.database_profile == p2
    mock_db.set_default_profile.assert_called_once_with("sqlite_two")

