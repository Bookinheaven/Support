"""Tests for persistent query history store and slash command workflow."""

import json
from io import StringIO
from unittest.mock import patch, MagicMock
import pytest
from rich.console import Console

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.history.models import QueryHistoryEntry
from support.history.store import QueryHistoryStore
from support.history.commands import handle_history_command
from support.workspace.commands import execute_slash_command
from support.workspace.state import WorkspaceSessionState
from support.schema.models import DatabaseSchema, TableSchema, ColumnSchema


@pytest.fixture
def temp_history_store(tmp_path):
    """Provide an isolated QueryHistoryStore in a temporary directory."""
    return QueryHistoryStore(config_dir=tmp_path, max_entries=5)


@pytest.fixture
def sample_session_state():
    """Provide a mock WorkspaceSessionState with postgres profile and cached schema."""
    state = WorkspaceSessionState()
    prof = ConnectionProfile(
        name="primary_db",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="app_db",
        username="dbuser",
    )
    state.set_database("primary_db", profile=prof, status="Online · 20 ms")
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="app_db")
    users = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(100)", nullable=False),
        ],
    )
    schema.tables["users"] = users
    state.cached_schema = schema
    return state


def test_history_store_adds_and_preserves_order(temp_history_store):
    """Verify entries are stored in chronological order with incrementing IDs."""
    e1 = temp_history_store.add_entry(
        question="Show all users",
        sql="SELECT id, username FROM users;",
        database="primary_db",
        dialect="postgres",
        is_validated=True,
    )
    e2 = temp_history_store.add_entry(
        question="Count all users",
        sql="SELECT COUNT(*) FROM users;",
        database="primary_db",
        dialect="postgres",
        is_validated=True,
    )

    entries = temp_history_store.list_entries()
    assert len(entries) == 2
    assert entries[0].id == 1
    assert entries[0].question == "Show all users"
    assert entries[1].id == 2
    assert entries[1].question == "Count all users"


def test_history_bounded_retention_limit(temp_history_store):
    """Verify history store respects bounded retention limit and evicts oldest entries."""
    # Store configured with max_entries=5
    for i in range(1, 9):
        temp_history_store.add_entry(
            question=f"Question {i}",
            sql=f"SELECT {i};",
            database="testdb",
            dialect="postgres",
        )

    entries = temp_history_store.list_entries()
    assert len(entries) == 5
    # Should contain items 4 through 8
    assert entries[0].question == "Question 4"
    assert entries[-1].question == "Question 8"


def test_history_never_stores_result_rows_or_credentials(temp_history_store):
    """Verify neither query result rows nor database passwords/credentials are written to disk."""
    temp_history_store.add_entry(
        question="List confidential users",
        sql="SELECT id, username FROM users WHERE role = 'admin';",
        database="secure_db",
        dialect="postgres",
        explanation="Lists admins",
        execution_status="Executed (2 rows in 15 ms)",
        duration_ms=15.0,
        row_count=2,
    )

    raw_json = temp_history_store.history_file.read_text(encoding="utf-8")
    parsed = json.loads(raw_json)

    # Check model fields
    entry_dict = parsed[0]
    assert "rows" not in entry_dict
    assert "result_rows" not in entry_dict
    assert "password" not in entry_dict
    assert "credentials" not in entry_dict
    assert "api_key" not in entry_dict
    assert entry_dict["row_count"] == 2
    assert entry_dict["duration_ms"] == 15.0


def test_history_update_last_execution(temp_history_store):
    """Verify update_last_execution updates the existing draft's outcome in place."""
    temp_history_store.add_entry(
        question="Count orders",
        sql="SELECT COUNT(*) FROM orders;",
        database="testdb",
        dialect="postgres",
        execution_status="Drafted (pending approval)",
    )

    updated = temp_history_store.update_last_execution(
        sql="SELECT COUNT(*) FROM orders;",
        execution_status="Executed (1 rows in 22 ms)",
        duration_ms=22.4,
        row_count=1,
    )
    assert updated is True

    entries = temp_history_store.list_entries()
    assert len(entries) == 1
    assert entries[0].execution_status == "Executed (1 rows in 22 ms)"
    assert entries[0].row_count == 1
    assert entries[0].duration_ms == 22.4


def test_history_clear_with_confirmation(temp_history_store, sample_session_state):
    """Verify /history clear prompts for confirmation and empties store when confirmed."""
    temp_history_store.add_entry(
        question="Test query",
        sql="SELECT 1;",
        database="testdb",
        dialect="postgres",
    )
    assert len(temp_history_store.list_entries()) == 1

    # User confirms with 'y'
    handle_history_command(
        state=sample_session_state,
        store=temp_history_store,
        arg="clear",
        user_input_func=lambda _: "y",
    )
    assert len(temp_history_store.list_entries()) == 0


def test_history_clear_declined(temp_history_store, sample_session_state):
    """Verify /history clear does not delete entries when declined."""
    temp_history_store.add_entry(
        question="Test query",
        sql="SELECT 1;",
        database="testdb",
        dialect="postgres",
    )
    handle_history_command(
        state=sample_session_state,
        store=temp_history_store,
        arg="clear",
        user_input_func=lambda _: "n",
    )
    assert len(temp_history_store.list_entries()) == 1


def test_history_disabling_configuration(tmp_path):
    """Verify history store gracefully disables when configured off."""
    store = QueryHistoryStore(config_dir=tmp_path, enabled=False)
    assert store.enabled is False
    res = store.add_entry(question="Q", sql="SELECT 1;", database="d", dialect="postgres")
    assert res is None
    assert store.list_entries() == []


def test_history_corrupt_file_graceful_recovery(tmp_path):
    """Verify malformed JSON file does not crash history store and returns empty list."""
    store = QueryHistoryStore(config_dir=tmp_path)
    store.history_file.write_text("{malformed: corrupted json [", encoding="utf-8")
    assert store.list_entries() == []


def test_history_use_restores_draft_with_dialect_check(temp_history_store, sample_session_state):
    """Verify /history use restores draft, validates dialect, re-validates schema, and asks approval."""
    temp_history_store.add_entry(
        question="Get users",
        sql="SELECT id, username FROM users LIMIT 10;",
        database="primary_db",
        dialect="postgres",
        is_validated=True,
    )

    test_console = Console(file=StringIO(), color_system=None)
    mock_input = MagicMock(return_value="n")  # Decline execution

    with patch("support.history.commands.console", test_console), \
         patch("support.query.display.console", test_console):
        handle_history_command(
            state=sample_session_state,
            store=temp_history_store,
            arg="use 1",
            user_input_func=mock_input,
        )

    # Candidate draft was restored in state
    assert sample_session_state.last_query_draft is not None
    assert sample_session_state.last_query_draft.sql == "SELECT id, username FROM users LIMIT 10;"
    assert sample_session_state.last_query_draft.is_validated is True
    # Fresh approval was requested
    mock_input.assert_called_once()
    assert "Execute this read-only query?" in mock_input.call_args[0][0]


def test_history_use_blocks_dialect_mismatch(temp_history_store, sample_session_state):
    """Verify /history use refuses to restore a query from an incompatible dialect."""
    temp_history_store.add_entry(
        question="T-SQL query",
        sql="SELECT TOP 5 id FROM users;",
        database="sqlserver_db",
        dialect="mssql",  # Mismatch with active 'postgres'
        is_validated=True,
    )

    test_console = Console(file=StringIO(), color_system=None)
    with patch("support.history.commands.console", test_console):
        handle_history_command(
            state=sample_session_state,
            store=temp_history_store,
            arg="use 1",
            user_input_func=lambda _: "y",
        )

    out = test_console.file.getvalue()
    assert "Dialect mismatch" in out
    assert "mssql" in out
    assert "postgres" in out


def test_history_command_in_dispatcher(sample_session_state):
    """Verify /history is recognized by execute_slash_command."""
    test_console = Console(file=StringIO(), color_system=None)
    with patch("support.history.commands.console", test_console):
        handled = execute_slash_command("/history", state=sample_session_state)
        assert handled is False  # Loop continues
        out = test_console.file.getvalue()
        assert "Recent Query History" in out or "No query history" in out
