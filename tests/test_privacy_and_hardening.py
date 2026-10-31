"""Tests for sensitive data protection, metadata queries, wide result presentation, and timeout safeguards."""

from unittest.mock import MagicMock
import pytest
from sqlalchemy import create_engine, text

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.query.display import display_query_result
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.privacy import (
    get_table_sensitive_columns,
    identify_sensitive_projections,
    is_sensitive_column_name,
    redact_sensitive_result_rows,
    try_expand_wildcard_projections,
)
from support.query.validator import check_schema_alignment, validate_sql_query
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.workspace.execution_commands import handle_run_command
from support.workspace.state import WorkspaceSessionState
import sqlglot


@pytest.fixture
def sensitive_users_schema():
    """Fixture providing a database schema with both sensitive and non-sensitive fields."""
    users_table = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="password_hash", data_type="VARCHAR(255)", nullable=False),
            ColumnSchema(name="api_token", data_type="VARCHAR(64)", nullable=True),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False),
        ],
        primary_key=["id"],
    )
    tokens_table = TableSchema(
        name="tokens",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER", nullable=False),
            ColumnSchema(name="refresh_token", data_type="VARCHAR(255)", nullable=False),
            ColumnSchema(name="expires_at", data_type="TIMESTAMP", nullable=False),
        ],
        primary_key=["id"],
    )
    return DatabaseSchema(
        dialect=Dialect.POSTGRES,
        tables={"users": users_table, "tokens": tokens_table},
    )


def test_sensitive_field_recognition():
    """Verify centralized detection identifies various credential, token, and secret fields."""
    sensitive_samples = [
        "password",
        "PASSWORD",
        "password_hash",
        "passwd",
        "pwd",
        "salt",
        "auth_token",
        "access_token",
        "refresh_token",
        "session_token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "private_key",
        "privkey",
        "session_id",
        "cookie",
    ]
    for name in sensitive_samples:
        assert is_sensitive_column_name(name) is True, f"Expected {name} to be classified as sensitive"

    safe_samples = [
        "id",
        "username",
        "email",
        "created_at",
        "updated_at",
        "title",
        "description",
        "status",
        "total_amount",
        "order_number",
    ]
    for name in safe_samples:
        assert is_sensitive_column_name(name) is False, f"Expected {name} to be classified as non-sensitive"


def test_schema_identifies_table_sensitive_columns(sensitive_users_schema):
    """Verify get_table_sensitive_columns extracts sensitive fields from table schema."""
    users_tbl = sensitive_users_schema.tables["users"]
    sensitive_cols = get_table_sensitive_columns(users_tbl)
    assert "password_hash" in sensitive_cols
    assert "api_token" in sensitive_cols
    assert "username" not in sensitive_cols
    assert "id" not in sensitive_cols


def test_validator_rejects_query_projecting_sensitive_fields(sensitive_users_schema):
    """Verify validator rejects queries explicitly selecting passwords or credentials."""
    sql = "SELECT id, username, password_hash FROM users WHERE id = 1"
    is_valid, msg = validate_sql_query(sql, dialect=Dialect.POSTGRES, schema=sensitive_users_schema)
    assert is_valid is False
    assert "sensitive credential/authentication field(s)" in msg
    assert "password_hash" in msg


def test_validator_rejects_aliased_sensitive_fields(sensitive_users_schema):
    """Verify SQLGlot AST inspection catches sensitive fields disguised under innocent aliases."""
    sql = "SELECT u.id, u.username, u.password_hash AS code_snippet FROM users u"
    is_valid, msg = validate_sql_query(sql, dialect=Dialect.POSTGRES, schema=sensitive_users_schema)
    assert is_valid is False
    assert "sensitive credential/authentication field(s)" in msg
    assert "code_snippet" in msg or "password_hash" in msg


def test_validator_rejects_unconstrained_wildcard_on_sensitive_table(sensitive_users_schema):
    """Verify validator rejects SELECT * when the referenced table contains sensitive fields."""
    sql = "SELECT * FROM users LIMIT 10"
    is_valid, msg = validate_sql_query(sql, dialect=Dialect.POSTGRES, schema=sensitive_users_schema)
    assert is_valid is False
    assert "Wildcard projection 'SELECT *' is prohibited on tables containing sensitive fields" in msg


def test_generator_deterministically_expands_wildcard_to_safe_columns(sensitive_users_schema):
    """Verify QueryGenerator transforms SELECT * into explicit non-sensitive columns."""
    ast = sqlglot.parse_one("SELECT * FROM users LIMIT 10", read="postgres")
    expanded = try_expand_wildcard_projections(ast, schema=sensitive_users_schema, dialect_str="postgres")
    assert expanded is not None

    expanded_sql = expanded.sql(dialect="postgres")
    assert "password_hash" not in expanded_sql
    assert "api_token" not in expanded_sql
    assert "id" in expanded_sql
    assert "username" in expanded_sql
    assert "email" in expanded_sql
    assert "created_at" in expanded_sql


def test_run_command_prevents_executing_unapproved_or_modified_query(sensitive_users_schema):
    """Verify /run strictly passes the exact approved SQL to the executor."""
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(active_database="pg_test", database_profile=profile)
    state.cached_schema = sensitive_users_schema

    draft = QueryDraft(
        question="List safe users",
        sql="SELECT id, username, email FROM users LIMIT 5;",
        explanation="Safe projection",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    state.last_query_draft = draft

    captured_sql = None

    def mock_exec(profile, password, sql, config=None):
        nonlocal captured_sql
        captured_sql = sql
        return QueryResult(columns=["id", "username", "email"], rows=[[1, "user1", "u1@test.com"]], row_count=1)

    mock_executor = MagicMock()
    mock_executor.execute.side_effect = mock_exec

    handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "yes")
    assert captured_sql == draft.sql


def test_redact_sensitive_result_rows_defense_in_depth():
    """Verify result-level redaction catches and redacts sensitive columns even if returned."""
    columns = ["id", "username", "password_hash", "access_token"]
    # Synthetic / fake test values
    rows = [
        [1, "alice", "$2b$12$e7k/FakeSaltFakeHashExampleValue999", "fake_tok_abc123"],
        [2, "bob", "$2b$12$a1b/SyntheticHashExampleValue888", "fake_tok_def456"],
    ]

    redacted_rows, redacted_cols = redact_sensitive_result_rows(columns=columns, rows=rows)
    assert "password_hash" in redacted_cols
    assert "access_token" in redacted_cols

    for r in redacted_rows:
        assert r[0] in (1, 2)
        assert r[1] in ("alice", "bob")
        assert r[2] == "[REDACTED]"
        assert r[3] == "[REDACTED]"


def test_system_metadata_tables_pass_schema_verification(sensitive_users_schema):
    """Verify information_schema.tables is recognized as a valid metadata object."""
    sql = "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'public'"
    parsed = sqlglot.parse_one(sql, read="postgres")

    (
        tables_ref,
        cols_ref,
        unknown_tables,
        unknown_cols,
        is_schema_ver,
        schema_msg,
    ) = check_schema_alignment(parsed, schema=sensitive_users_schema)

    assert unknown_tables == []
    assert is_schema_ver is True
    assert "Verified" in schema_msg


def test_unknown_application_table_rejected_even_when_metadata_referenced(sensitive_users_schema):
    """Verify unknown application tables cannot sneak through simply by joining information_schema."""
    sql = "SELECT t.table_name FROM information_schema.tables t JOIN fake_application_table f ON t.table_name = f.name"
    parsed = sqlglot.parse_one(sql, read="postgres")

    (
        tables_ref,
        cols_ref,
        unknown_tables,
        unknown_cols,
        is_schema_ver,
        schema_msg,
    ) = check_schema_alignment(parsed, schema=sensitive_users_schema)

    assert "fake_application_table" in unknown_tables
    assert is_schema_ver is False
    assert "References tables not in schema: fake_application_table" in schema_msg


def test_sqlite_metadata_views_recognized():
    """Verify sqlite_master passes schema alignment checks."""
    dummy_schema = DatabaseSchema(dialect=Dialect.SQLITE, tables={})
    sql = "SELECT name, type FROM sqlite_master WHERE type = 'table'"
    parsed = sqlglot.parse_one(sql, read="sqlite")

    (
        tables_ref,
        cols_ref,
        unknown_tables,
        unknown_cols,
        is_schema_ver,
        schema_msg,
    ) = check_schema_alignment(parsed, schema=dummy_schema)

    assert unknown_tables == []
    assert is_schema_ver is True


def test_display_wide_result_compact_and_vertical():
    """Verify wide results omit excess columns in table view and show all columns in vertical view."""
    many_cols = [f"col_{i}" for i in range(10)]
    row_data = [[f"val_{i}_{r}" for i in range(10)] for r in range(3)]
    res = QueryResult(
        columns=many_cols,
        rows=row_data,
        row_count=3,
        duration_ms=10.0,
        executed_sql="SELECT * FROM wide_table",
        database="testdb",
        dialect="postgres",
    )

    # 1. Default table view (compact)
    display_query_result(res, view_mode="table")

    # 2. Vertical view (all columns rendered without omission)
    display_query_result(res, view_mode="vertical")

    # 3. Wide view
    display_query_result(res, view_mode="wide")


def test_sql_server_timeout_distinguishes_query_from_lock_timeout():
    """Verify SQL Server execution reports query timeout (pymssql) distinct from lock timeout."""
    executor = ReadOnlyQueryExecutor()
    profile = ConnectionProfile(
        name="mssql_test",
        dialect=Dialect.MSSQL,
        database="testdb",
        options={"allow_unrestricted_exec": True},
    )

    mock_conn = MagicMock()
    safeguard_str = executor._apply_safeguards_and_timeout(
        mock_conn, Dialect.MSSQL, ExecutionConfig(timeout_seconds=5.0), profile
    )

    assert "query_timeout=5s" in safeguard_str
    assert "lock_timeout=5000ms" in safeguard_str


def test_run_command_blocks_sensitive_projections_at_execution_time(sensitive_users_schema):
    """Verify /run blocks any draft containing sensitive projections before execution."""
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(active_database="pg_test", database_profile=profile)
    state.cached_schema = sensitive_users_schema

    # Draft with sensitive column
    draft = QueryDraft(
        question="Show passwords",
        sql="SELECT password_hash FROM users LIMIT 1;",
        explanation="Passwords",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    state.last_query_draft = draft

    mock_executor = MagicMock()
    executed = handle_run_command(state=state, executor=mock_executor, user_input_func=lambda _: "yes")
    assert executed is False
    mock_executor.execute.assert_not_called()


def test_delete_and_update_with_subquery_validation(sensitive_users_schema):
    """Verify DELETE and UPDATE statements with subqueries in WHERE clause validate without AttributeError."""
    del_sql = "DELETE FROM users WHERE id IN (SELECT id FROM users LIMIT 5)"
    res_del = validate_sql_query(del_sql, dialect="postgres", schema=sensitive_users_schema, allow_mutations=True)
    assert res_del.is_valid is True
    assert res_del.operation == "DELETE"

    upd_sql = "UPDATE users SET email = 'test@example.com' WHERE id IN (SELECT id FROM users LIMIT 5)"
    res_upd = validate_sql_query(upd_sql, dialect="postgres", schema=sensitive_users_schema, allow_mutations=True)
    assert res_upd.is_valid is True
    assert res_upd.operation == "UPDATE"


def test_dml_returning_sensitive_column_rejected(sensitive_users_schema):
    """Verify DML statements with RETURNING clauses projecting sensitive fields are rejected."""
    del_sens = "DELETE FROM users WHERE id = 1 RETURNING password_hash"
    res = validate_sql_query(del_sens, dialect="postgres", schema=sensitive_users_schema, allow_mutations=True)
    assert res.is_valid is False
    assert "password_hash" in res.status_message

