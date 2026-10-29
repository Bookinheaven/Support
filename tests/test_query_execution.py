"""Tests for controlled, explicitly approved read-only query execution and safeguards."""

import time
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine, text

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.query.display import display_execution_preview, display_query_result
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.models import QueryDraft
from support.workspace.execution_commands import handle_run_command
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def sample_sqlite_profile(tmp_path):
    """Fixture providing a valid SQLite connection profile with sample data."""
    db_file = tmp_path / "test_exec.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)"))
        conn.execute(
            text(
                "INSERT INTO products (name, price) VALUES "
                "('Widget', 9.99), ('Gadget', 19.99), ('Doohickey', 29.99)"
            )
        )
        conn.commit()
    engine.dispose()

    return ConnectionProfile(
        name="test_sqlite",
        dialect=Dialect.SQLITE,
        database=str(db_file),
    )


@pytest.fixture
def valid_draft():
    """Fixture providing a validated and schema-verified query draft."""
    return QueryDraft(
        question="List all products",
        sql="SELECT id, name, price FROM products ORDER BY id ASC;",
        explanation="Lists all products sorted by ID.",
        dialect="sqlite",
        validation_status="Passed",
        is_validated=True,
        is_parsed=True,
        schema_status="Verified against active database schema",
        is_schema_verified=True,
        execution_status="Not performed",
    )


def test_run_command_requires_active_database():
    """Verify /run rejects execution when no active database is selected."""
    state = WorkspaceSessionState()
    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_blocks_mongodb():
    """Verify /run blocks MongoDB profiles from SQL execution pipeline."""
    state = WorkspaceSessionState(
        active_database="mongo_dev",
        database_profile=ConnectionProfile(name="mongo_dev", dialect=Dialect.MONGODB, database="admin"),
    )
    state.last_query_draft = QueryDraft(
        question="Find users",
        sql="db.users.find()",
        explanation="Find all",
        dialect="mongodb",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_requires_active_draft(sample_sqlite_profile):
    """Verify /run rejects execution when no query draft exists."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    state.last_query_draft = None
    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_rejects_dialect_mismatch(sample_sqlite_profile, valid_draft):
    """Verify /run rejects drafts generated for a different database dialect."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    # Draft is marked for postgres, while profile is sqlite
    valid_draft.dialect = "postgres"
    state.last_query_draft = valid_draft

    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_rejects_unvalidated_draft(sample_sqlite_profile, valid_draft):
    """Verify /run rejects execution if query failed validation."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    valid_draft.is_validated = False
    valid_draft.validation_status = "Rejected: contains DROP TABLE"
    state.last_query_draft = valid_draft

    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_rejects_schema_unverified_draft(sample_sqlite_profile, valid_draft):
    """Verify /run rejects execution if schema alignment is unverified."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    valid_draft.is_schema_verified = False
    valid_draft.schema_status = "Warning: unverified tables"
    state.last_query_draft = valid_draft

    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_rejects_unknown_tables(sample_sqlite_profile, valid_draft):
    """Verify /run rejects execution if unknown/hallucinated tables were detected."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    valid_draft.unknown_tables = ["nonexistent_orders"]
    state.last_query_draft = valid_draft

    executed = handle_run_command(state=state, user_input_func=lambda _: "y")
    assert executed is False


def test_run_command_revalidates_and_rejects_if_malformed(sample_sqlite_profile, valid_draft):
    """Verify /run performs pre-execution revalidation before touching the database."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    # Tamper with the SQL on the draft so it fails revalidation
    valid_draft.sql = "DELETE FROM products WHERE id = 1;"
    state.last_query_draft = valid_draft

    mock_executor = MagicMock()
    executed = handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "y")
    assert executed is False
    mock_executor.execute.assert_not_called()


def test_run_command_confirmation_defaults_to_no_and_cancels(sample_sqlite_profile, valid_draft):
    """Verify /run defaults to No and cancels execution without database interaction."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    state.last_query_draft = valid_draft

    mock_executor = MagicMock()

    # Case 1: Empty input (Enter pressed)
    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "")
    mock_executor.execute.assert_not_called()

    # Case 2: Explicit "n" or "no"
    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "n")
    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "no")
    mock_executor.execute.assert_not_called()

    # Case 3: Random characters
    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "maybe")
    mock_executor.execute.assert_not_called()


def test_run_command_explicit_confirmation_executes(sample_sqlite_profile, valid_draft):
    """Verify /run executes only when explicit approval ('y' or 'yes') is given."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    state.last_query_draft = valid_draft

    mock_executor = MagicMock()
    mock_executor.execute.return_value = QueryResult(
        columns=["id", "name", "price"],
        rows=[[1, "Widget", 9.99]],
        row_count=1,
        duration_ms=5.0,
        executed_sql=valid_draft.sql,
        database="test_sqlite",
        dialect="sqlite",
    )

    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "y")
    mock_executor.execute.assert_called_once()
    assert "Executed" in valid_draft.execution_status


def test_run_command_approved_sql_strictly_matches_executed_sql(sample_sqlite_profile, valid_draft):
    """Verify the approved SQL query string is passed unchanged to the executor."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    state.last_query_draft = valid_draft

    captured_sql = None

    def capture_execute(profile, password, sql, config=None):
        nonlocal captured_sql
        captured_sql = sql
        return QueryResult(columns=["id"], rows=[[1]], row_count=1, database="test", dialect="sqlite")

    mock_executor = MagicMock()
    mock_executor.execute.side_effect = capture_execute

    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "yes")
    assert captured_sql == valid_draft.sql


def test_draft_invalidated_when_switching_database(sample_sqlite_profile, valid_draft):
    """Verify switching the active database profile clears the active query draft."""
    state = WorkspaceSessionState(
        active_database="test_sqlite",
        database_profile=sample_sqlite_profile,
    )
    state.last_query_draft = valid_draft
    assert state.last_query_draft is not None

    # Switch database
    state.set_database("other_db", ConnectionProfile(name="other_db", dialect=Dialect.POSTGRES, database="other"))
    assert state.last_query_draft is None

    # /run must now reject execution
    mock_executor = MagicMock()
    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "y")
    mock_executor.execute.assert_not_called()


def test_sqlite_readonly_guard_enforced_by_executor(sample_sqlite_profile):
    """Verify SQLite PRAGMA query_only prevents any write operations."""
    executor = ReadOnlyQueryExecutor()

    # Attempt an INSERT through executor
    res = executor.execute(
        sample_sqlite_profile,
        password=None,
        sql="INSERT INTO products (name, price) VALUES ('Illegal', 99.0)",
    )
    assert res.is_success is False
    assert res.is_readonly_error is True
    assert "read-only session" in res.error_message


def test_sqlite_progress_handler_timeout_interrupts_query(sample_sqlite_profile):
    """Verify SQLite queries exceeding timeout are interrupted cleanly."""
    executor = ReadOnlyQueryExecutor()
    cfg = ExecutionConfig(timeout_seconds=0.000001, max_rows=100000)

    # Infinite recursive CTE to simulate runaway query
    runaway_sql = "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt) SELECT * FROM cnt"
    res = executor.execute(sample_sqlite_profile, password=None, sql=runaway_sql, config=cfg)

    assert res.is_success is False
    assert res.is_timeout is True
    assert "timed out after 0.0s" in res.error_message


def test_executor_bounded_fetching_and_truncation(tmp_path):
    """Verify executor fetches bounded batches and accurately detects truncation."""
    db_file = tmp_path / "bounded.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE items (id INT)"))
        # Insert 15 items
        for i in range(15):
            conn.execute(text(f"INSERT INTO items VALUES ({i})"))
        conn.commit()
    engine.dispose()

    profile = ConnectionProfile(name="bounded_db", dialect=Dialect.SQLITE, database=str(db_file))
    executor = ReadOnlyQueryExecutor()

    # Set max_rows = 10 (fewer than total 15)
    cfg = ExecutionConfig(max_rows=10, batch_size=5)
    res = executor.execute(profile, password=None, sql="SELECT id FROM items ORDER BY id ASC", config=cfg)

    assert res.is_success is True
    assert res.row_count == 10
    assert len(res.rows) == 10
    assert res.is_truncated is True

    # Now set max_rows = 20 (more than total 15)
    cfg2 = ExecutionConfig(max_rows=20, batch_size=5)
    res2 = executor.execute(profile, password=None, sql="SELECT id FROM items ORDER BY id ASC", config=cfg2)

    assert res2.is_success is True
    assert res2.row_count == 15
    assert len(res2.rows) == 15
    assert res2.is_truncated is False


def test_executor_handles_empty_result_set(sample_sqlite_profile):
    """Verify executor handles queries returning zero rows cleanly."""
    executor = ReadOnlyQueryExecutor()
    res = executor.execute(
        sample_sqlite_profile,
        password=None,
        sql="SELECT id, name FROM products WHERE id = 99999",
    )
    assert res.is_success is True
    assert res.row_count == 0
    assert res.rows == []
    assert res.columns == ["id", "name"]
    assert res.is_truncated is False


def test_sql_server_fails_closed_on_privileged_account():
    """Verify SQL Server fails closed when connection uses sysadmin or db_owner without denydatawriter."""
    profile = ConnectionProfile(name="dev_mssql", dialect=Dialect.MSSQL, database="testdb")

    mock_conn = MagicMock()
    # Mock sysadmin role check to return 1 (True)
    mock_conn.execute.side_effect = [
        MagicMock(scalar=lambda: 1),  # sysadmin = 1
        MagicMock(scalar=lambda: 1),  # db_owner = 1
        MagicMock(scalar=lambda: 0),  # denydatawriter = 0
    ]

    executor = ReadOnlyQueryExecutor()
    with pytest.raises(PermissionError) as exc_info:
        executor._apply_safeguards_and_timeout(mock_conn, Dialect.MSSQL, ExecutionConfig(), profile)

    assert "unrestricted write privileges" in str(exc_info.value)
    assert "db_denydatawriter" in str(exc_info.value)


def test_display_query_result_renders_all_states(capsys):
    """Verify terminal rendering for successful, empty, truncated, and failed execution states."""
    # 1. Success state
    res_success = QueryResult(
        columns=["id", "name", "price"],
        rows=[[1, "Widget", 9.99], [2, "Gadget", 19.99]],
        row_count=2,
        duration_ms=12.5,
        executed_sql="SELECT * FROM products",
        database="testdb",
        dialect="sqlite",
    )
    display_query_result(res_success)

    # 2. Empty state
    res_empty = QueryResult(
        columns=["id", "name"],
        rows=[],
        row_count=0,
        duration_ms=4.2,
        executed_sql="SELECT * FROM products WHERE id = -1",
        database="testdb",
        dialect="sqlite",
    )
    display_query_result(res_empty)

    # 3. Truncated state
    res_trunc = QueryResult(
        columns=["id"],
        rows=[[i] for i in range(100)],
        row_count=100,
        is_truncated=True,
        duration_ms=45.0,
        executed_sql="SELECT id FROM big_table",
        database="testdb",
        dialect="sqlite",
    )
    display_query_result(res_trunc)

    # 4. Error state
    res_err = QueryResult(
        error_message="Execution timed out after 10.0s.",
        duration_ms=10000.0,
        executed_sql="SELECT * FROM slow_query",
        database="testdb",
        dialect="sqlite",
    )
    display_query_result(res_err)
