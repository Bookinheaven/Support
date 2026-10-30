"""Focused unit tests for live database and model status verification in Support workspace."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from support.db.models import ConnectionProfile, Dialect
from support.db.health import HealthCheckResult
from support.db.service import DatabaseService
from support.llm.models import ProviderStatus, ProviderValidationResult
from support.llm.service import LLMService
from support.workspace.checker import WorkspaceStatusChecker
from support.workspace.commands import execute_slash_command, handle_status
from support.workspace.display import (
    display_status_overview,
    format_database_status_line,
    format_model_status_line,
    get_prompt_html,
)
from support.workspace.session import run_workspace_session
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def sample_state():
    """Create a test session state with Postgres profile and Ollama model."""
    profile = ConnectionProfile(
        name="athena",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="athena",
        username="postgres",
    )
    state = WorkspaceSessionState(
        active_database="athena",
        database_profile=profile,
        database_status="Checking...",
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
        provider_status="Checking...",
    )
    return state


def test_workspace_startup_remains_fast_and_independent_of_network():
    """Verify initialize() loads local metadata only and startup takes < 50ms."""
    mock_db = MagicMock(spec=DatabaseService)
    profile = ConnectionProfile(name="athena", dialect=Dialect.POSTGRES, database="athena")
    mock_db.list_profiles.return_value = [profile]

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5-coder:7b"
    mock_llm.get_config.return_value = mock_cfg

    start_time = time.perf_counter()
    state = WorkspaceSessionState.initialize(db_service=mock_db, llm_service=mock_llm)
    duration_ms = (time.perf_counter() - start_time) * 1000

    # Must complete almost instantaneously (well under 50ms)
    assert duration_ms < 50.0

    # No network probes during startup
    mock_db.list_profiles_with_status.assert_not_called()
    mock_llm.validate_provider.assert_not_called()

    # Initial state matches prompt specification
    assert state.active_database == "athena"
    assert state.database_status == "Checking..."
    assert state.active_provider == "ollama"
    assert state.active_model == "qwen2.5-coder:7b"
    assert state.provider_status == "Checking..."


def test_database_and_model_checks_proceed_independently(sample_state):
    """Verify slow database verification does not block or delay model verification."""
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_state.database_profile, None)

    mock_llm = MagicMock(spec=LLMService)

    # Slow DB check (sleeps 0.3s)
    def slow_db_check(*args, **kwargs):
        time.sleep(0.3)
        return HealthCheckResult(status="ONLINE", latency_ms=84.0)

    # Fast model check (instant)
    def fast_llm_check(*args, **kwargs):
        return ProviderValidationResult(
            provider="ollama",
            status=ProviderStatus.AVAILABLE,
            message="Ollama operational",
            latency_ms=15.0,
        )

    mock_llm.validate_provider.side_effect = fast_llm_check

    with patch("support.workspace.checker.check_connection_health", side_effect=slow_db_check):
        checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
        checker.start_all()

        # Within 0.1s, model check should have already finished while DB is still checking
        time.sleep(0.1)
        assert sample_state.provider_status == "Available"
        assert sample_state.database_status == "Checking..."

        # Wait for slow DB check to complete
        checker.wait_completion(timeout=2.0)
        assert sample_state.database_status == "Online · 84 ms"

    checker.shutdown()


def test_status_transitions_database_outcomes(sample_state):
    """Verify database health check status mappings for Online, Auth Failed, Timeout, Offline."""
    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_state.database_profile, None)
    mock_llm = MagicMock(spec=LLMService)

    test_cases = [
        (HealthCheckResult(status="ONLINE", latency_ms=42.0), "Online · 42 ms"),
        (HealthCheckResult(status="AUTH FAILED", hint="Password rejected"), "Authentication failed"),
        (HealthCheckResult(status="TIMEOUT", hint="Connection timed out"), "Timeout"),
        (HealthCheckResult(status="OFFLINE", hint="Host unreachable"), "Offline"),
    ]

    for health_result, expected_status in test_cases:
        with patch("support.workspace.checker.check_connection_health", return_value=health_result):
            checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
            checker.check_database()
            checker.wait_completion(timeout=1.0)
            assert sample_state.database_status == expected_status
            checker.shutdown()


def test_status_transitions_model_outcomes(sample_state):
    """Verify model provider status mappings for Available, Missing Model, Unreachable, Auth Failed."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    test_cases = [
        (
            ProviderValidationResult(provider="ollama", status=ProviderStatus.AVAILABLE, message="OK"),
            "Available",
        ),
        (
            ProviderValidationResult(
                provider="ollama",
                status=ProviderStatus.MODEL_UNAVAILABLE,
                message="Model not pulled",
                hint="Run: ollama pull qwen2.5-coder:7b",
            ),
            "Model 'qwen2.5-coder:7b' not installed",
        ),
        (
            ProviderValidationResult(
                provider="ollama",
                status=ProviderStatus.UNREACHABLE,
                message="Cannot reach Ollama",
            ),
            "Provider unreachable",
        ),
        (
            ProviderValidationResult(
                provider="gemini",
                status=ProviderStatus.AUTH_FAILED,
                message="Invalid API Key",
            ),
            "Authentication failed",
        ),
    ]

    for val_result, expected_status in test_cases:
        mock_llm.validate_provider.return_value = val_result
        checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
        checker.check_model()
        checker.wait_completion(timeout=1.0)
        assert sample_state.provider_status == expected_status
        checker.shutdown()


def test_stale_database_check_rejection_on_profile_change(sample_state):
    """Verify in-flight check for a previous database profile cannot overwrite a newly selected database."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    db_started_event = threading.Event()
    db_finish_event = threading.Event()

    def slow_check(*args, **kwargs):
        db_started_event.set()
        db_finish_event.wait(timeout=2.0)
        return HealthCheckResult(status="ONLINE", latency_ms=99.0, is_online=True)

    with patch("support.workspace.checker.check_connection_health", side_effect=slow_check):
        checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
        checker.check_database()

        # Wait until background worker has picked up the check
        db_started_event.wait(timeout=1.0)
        assert sample_state.database_status == "Checking..."

        # User switches active database while check is in-flight
        new_profile = ConnectionProfile(name="production_mysql", dialect=Dialect.MYSQL, database="proddb")
        sample_state.set_database(
            name="production_mysql",
            profile=new_profile,
            status="Configured (untested)",
        )

        # Allow the slow check for "athena" to finish
        db_finish_event.set()
        checker.wait_completion(timeout=1.0)

        # The stale result for "athena" MUST NOT overwrite "production_mysql"
        assert sample_state.active_database == "production_mysql"
        assert sample_state.database_status == "Configured (untested)"

    checker.shutdown()


def test_stale_model_check_rejection_on_model_change(sample_state):
    """Verify in-flight check for previous model cannot overwrite a newly selected model."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    model_started_event = threading.Event()
    model_finish_event = threading.Event()

    def slow_llm_check(*args, **kwargs):
        model_started_event.set()
        model_finish_event.wait(timeout=2.0)
        return ProviderValidationResult(
            provider="ollama",
            status=ProviderStatus.AVAILABLE,
            message="OK",
        )

    mock_llm.validate_provider.side_effect = slow_llm_check

    checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
    checker.check_model()

    model_started_event.wait(timeout=1.0)
    assert sample_state.provider_status == "Checking..."

    # User changes model in state
    sample_state.set_model("gemini", "gemini-2.0-flash", status="Configured (untested)")

    # Complete the stale check for ollama/qwen2.5-coder:7b
    model_finish_event.set()
    checker.wait_completion(timeout=1.0)

    # Must preserve new model selection and reject stale result
    assert sample_state.active_provider == "gemini"
    assert sample_state.active_model == "gemini-2.0-flash"
    assert sample_state.provider_status == "Configured (untested)"

    checker.shutdown()


def test_status_command_overview_and_refresh(sample_state):
    """Verify /status displays overview and /status refresh re-runs connectivity checks."""
    mock_checker = MagicMock()
    mock_checker.refresh = MagicMock()
    mock_checker.wait_completion = MagicMock()

    # 1. Plain /status
    exit_requested = execute_slash_command(
        command_str="/status",
        state=sample_state,
        checker=mock_checker,
    )
    assert exit_requested is False
    mock_checker.refresh.assert_not_called()

    # 2. /status refresh
    exit_requested = execute_slash_command(
        command_str="/status refresh",
        state=sample_state,
        checker=mock_checker,
    )
    assert exit_requested is False
    mock_checker.refresh.assert_called_once()
    mock_checker.wait_completion.assert_called_once()


def test_live_updates_do_not_corrupt_typed_input():
    """Verify prompt_toolkit invalidate() during background status change preserves buffered typing."""
    profile = ConnectionProfile(name="athena", dialect=Dialect.POSTGRES, database="athena")
    state = WorkspaceSessionState(
        active_database="athena",
        database_profile=profile,
        database_status="Checking...",
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
        provider_status="Checking...",
    )

    with create_pipe_input() as pipe:
        session = PromptSession(input=pipe, output=DummyOutput())

        def on_change():
            if session.app and session.app.is_running:
                session.app.loop.call_soon_threadsafe(session.app.invalidate)

        state.add_listener(on_change)

        def bg_writer():
            # Send initial keystrokes while state is Checking...
            pipe.send_text("select * from ")
            time.sleep(0.05)

            # Background check finishes and updates state
            state.set_database_status("Online · 84 ms")
            state.set_model_status("Available")
            time.sleep(0.05)

            # Send rest of query and press Enter
            pipe.send_text("customers;\n")

        bg_thread = threading.Thread(target=bg_writer)
        bg_thread.start()

        # Prompt should execute and return the entire typed text without corruption or loss
        user_input = session.prompt(lambda: HTML(get_prompt_html(state, first_turn=True)))
        bg_thread.join()

        assert user_input == "select * from customers;"


def test_clean_shutdown_on_exit(sample_state):
    """Verify checker.shutdown() halts background threads cleanly."""
    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    checker = WorkspaceStatusChecker(sample_state, db_service=mock_db, llm_service=mock_llm)
    checker.start_all()

    assert not checker._shutdown_event.is_set()
    checker.shutdown()
    assert checker._shutdown_event.is_set()


def test_workspace_session_runs_and_exits_cleanly():
    """Verify run_workspace_session initializes checker, handles command, and shuts down."""
    mock_db = MagicMock(spec=DatabaseService)
    profile = ConnectionProfile(name="test_pg", dialect=Dialect.POSTGRES, database="testdb")
    mock_db.list_profiles.return_value = [profile]

    mock_llm = MagicMock(spec=LLMService)
    mock_cfg = MagicMock()
    mock_cfg.default_provider = "ollama"
    mock_cfg.default_model = "qwen2.5-coder:7b"
    mock_llm.get_config.return_value = mock_cfg

    # Provide an input function that immediately exits
    inputs = ["/status", "/exit"]

    def fake_input(prompt: str) -> str:
        return inputs.pop(0)

    # Should run and exit without hanging
    run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=fake_input)
