"""Focused tests for conversational query UX, adaptive rendering, and intelligent error recovery."""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.schema.models import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    TableSchema,
)
from support.llm.models import GenerationResponse
from support.query.display import (
    display_query_details,
    display_query_draft,
    display_query_result,
)
from support.query.executor import QueryResult, ReadOnlyQueryExecutor
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.repair import (
    build_repair_prompt,
    find_unambiguous_schema_match,
    is_recoverable_execution_error,
)
from support.workspace.commands import execute_slash_command
from support.workspace.execution_commands import (
    handle_conversational_execution,
    handle_run_command,
)
from support.workspace.general_commands import handle_details
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def mock_schema():
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="testdb")
    users = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR"),
            ColumnSchema(name="password_hash", data_type="VARCHAR"),
            ColumnSchema(name="email", data_type="VARCHAR"),
        ],
    )
    orders = TableSchema(
        name="orders",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER"),
            ColumnSchema(name="total_amount", data_type="NUMERIC"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
            )
        ],
    )
    schema.tables["users"] = users
    schema.tables["orders"] = orders
    return schema


@pytest.fixture
def sample_state(mock_schema):
    state = WorkspaceSessionState(
        active_database="testdb",
        database_profile=ConnectionProfile(
            name="testdb", dialect=Dialect.POSTGRES, database="testdb"
        ),
    )
    state.cached_schema = mock_schema
    return state


# ---------------------------------------------------------------------------
# 1. Conversational Query Workflow & Explicit Approval
# ---------------------------------------------------------------------------

def test_conversational_approval_executes_on_yes(sample_state):
    """Verify conversational workflow executes draft immediately when user approves with 'y'."""
    draft = QueryDraft(
        question="List all orders",
        sql="SELECT id, user_id, total_amount FROM orders LIMIT 5;",
        explanation="Lists up to 5 orders.",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    sample_state.last_query_draft = draft

    mock_executor = MagicMock()
    mock_executor.execute.return_value = QueryResult(
        executed_sql=draft.sql,
        columns=["id", "user_id", "total_amount"],
        rows=[(1, 10, 99.50)],
        row_count=1,
        duration_ms=12.0,
        dialect="postgres",
        database="testdb",
    )

    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        user_input_func=lambda _: "y",
        executor=mock_executor,
    )

    mock_executor.execute.assert_called_once()
    assert sample_state.last_query_result is not None
    assert sample_state.last_query_result.row_count == 1
    assert "Executed" in draft.execution_status


def test_conversational_approval_cancels_on_enter_or_no(sample_state):
    """Verify pressing Enter or entering 'n' never executes the query and retains the draft."""
    draft = QueryDraft(
        question="List all orders",
        sql="SELECT id, user_id, total_amount FROM orders LIMIT 5;",
        explanation="Lists up to 5 orders.",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    sample_state.last_query_draft = draft
    mock_executor = MagicMock()

    # Empty string (Enter)
    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        user_input_func=lambda _: "",
        executor=mock_executor,
    )
    mock_executor.execute.assert_not_called()
    assert sample_state.last_query_draft == draft

    # Explicit 'n'
    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        user_input_func=lambda _: "n",
        executor=mock_executor,
    )
    mock_executor.execute.assert_not_called()

    # Unexpected input
    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        user_input_func=lambda _: "maybe",
        executor=mock_executor,
    )
    mock_executor.execute.assert_not_called()


def test_unvalidated_or_unknown_table_drafts_never_prompt_execution(sample_state):
    """Verify drafts that failed validation or have unresolved schema references are never offered for execution."""
    invalid_draft = QueryDraft(
        question="Delete users",
        sql="DELETE FROM users;",
        explanation="Deletes users.",
        dialect="postgres",
        validation_status="Rejected: Mutation statement",
        is_validated=False,
        is_schema_verified=False,
    )
    mock_executor = MagicMock()
    mock_input = MagicMock(return_value="y")

    handle_conversational_execution(
        draft=invalid_draft,
        state=sample_state,
        user_input_func=mock_input,
        executor=mock_executor,
    )
    mock_input.assert_not_called()
    mock_executor.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Diagnostics & Details Command
# ---------------------------------------------------------------------------

def test_details_command_displays_diagnostics_without_cluttering_draft(sample_state):
    """Verify /details displays deep diagnostics while ordinary draft view remains concise."""
    draft = QueryDraft(
        question="List users",
        sql="SELECT id, username FROM users LIMIT 10;",
        explanation="Lists users.",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_parsed=True,
        schema_status="Verified against active database schema",
        is_schema_verified=True,
        execution_status="Not performed",
        schema_coverage="Relevant subset (2 of 2 tables)",
        tables_used=["users"],
    )
    sample_state.last_query_draft = draft

    # Test concise draft output
    draft_console = Console(file=StringIO(), color_system=None)
    with patch("support.query.display.console", draft_console):
        display_query_draft(draft, db_name="testdb", dialect_name="postgres")
        draft_text = draft_console.file.getvalue()
        assert "Query ready · Schema verified" in draft_text
        assert "Database: testdb (postgres) · Read-only: Enforced" in draft_text
        assert "Parsing: Successful" not in draft_text
        assert "Validation: Passed" not in draft_text
        assert "Schema Context:" not in draft_text

    # Test /details output
    details_console = Console(file=StringIO(), color_system=None)
    with patch("support.query.display.console", details_console):
        execute_slash_command("/details", state=sample_state)
        details_text = details_console.file.getvalue()
        assert "Query Draft Diagnostics" in details_text
        assert "Successful" in details_text
        assert "Passed" in details_text
        assert "users" in details_text
        assert "Relevant subset (2 of 2 tables)" in details_text


# ---------------------------------------------------------------------------
# 3. Adaptive Result Rendering
# ---------------------------------------------------------------------------

def test_adaptive_layout_chooses_table_for_narrow_results():
    """Verify adaptive rendering uses a normal table for results with <= 5 columns."""
    result = QueryResult(
        executed_sql="SELECT id, username FROM users LIMIT 2;",
        columns=["id", "username"],
        rows=[(1, "alice"), (2, "bob")],
        row_count=2,
        duration_ms=5.0,
        dialect="postgres",
        database="testdb",
    )
    test_console = Console(file=StringIO(), color_system=None, width=120)
    with patch("support.query.display.console", test_console):
        display_query_result(result, view_mode="auto")
        out = test_console.file.getvalue()
        # Should not display vertical Record header
        assert "Record View" not in out
        assert "alice" in out
        assert "bob" in out


def test_adaptive_layout_chooses_record_view_for_wide_results():
    """Verify adaptive rendering automatically uses record view when > 5 columns are returned."""
    cols = ["col1", "col2", "col3", "col4", "col5", "col6", "col7"]
    rows = [[f"val_{i}_{j}" for j in range(7)] for i in range(2)]
    result = QueryResult(
        executed_sql="SELECT * FROM wide_table LIMIT 2;",
        columns=cols,
        rows=rows,
        row_count=2,
        duration_ms=8.0,
        dialect="postgres",
        database="testdb",
    )
    test_console = Console(file=StringIO(), color_system=None, width=120)
    with patch("support.query.display.console", test_console):
        display_query_result(result, view_mode="auto")
        out = test_console.file.getvalue()
        assert "Record View" in out
        assert "Record 1 of 2" in out
        assert "col7" in out


def test_sensitive_field_redacted_in_all_views():
    """Verify password_hash and credential fields are redacted in both table and record views."""
    result = QueryResult(
        executed_sql="SELECT id, username, password_hash FROM users LIMIT 1;",
        columns=["id", "username", "password_hash"],
        rows=[(1, "alice", "argon2id$v=19$m=65536,t=3,p=4$secretpasswordhash")],
        row_count=1,
        duration_ms=6.0,
        dialect="postgres",
        database="testdb",
    )
    # 1. Table view
    t_console = Console(file=StringIO(), color_system=None)
    with patch("support.query.display.console", t_console):
        display_query_result(result, view_mode="table")
        t_out = t_console.file.getvalue()
        assert "[REDACTED]" in t_out
        assert "secretpasswordhash" not in t_out

    # 2. Record view
    v_console = Console(file=StringIO(), color_system=None)
    with patch("support.query.display.console", v_console):
        display_query_result(result, view_mode="vertical")
        v_out = v_console.file.getvalue()
        assert "[REDACTED]" in v_out
        assert "secretpasswordhash" not in v_out


def test_view_command_removed_from_dispatcher(sample_state):
    """Verify /view is no longer a recognized slash command."""
    test_console = Console(file=StringIO(), color_system=None)
    with patch("support.workspace.commands.console", test_console):
        execute_slash_command("/view vertical", state=sample_state)
        out = test_console.file.getvalue()
        assert "Unknown command" in out
        assert "/view vertical" in out


# ---------------------------------------------------------------------------
# 4. Deterministic Spelling & Schema Grounded Repair
# ---------------------------------------------------------------------------

def test_unambiguous_spelling_match():
    """Verify find_unambiguous_schema_match correctly resolves single typos and plurals."""
    schema_tables = ["users", "orders", "order_items", "products"]

    # Typo: 'user' -> 'users'
    match, is_unambiguous = find_unambiguous_schema_match("user", schema_tables)
    assert is_unambiguous is True
    assert match == "users"

    # Exact match
    match, is_unambiguous = find_unambiguous_schema_match("users", schema_tables)
    assert is_unambiguous is True
    assert match == "users"


def test_refuse_to_guess_ambiguous_identifiers():
    """Verify matcher refuses to guess when multiple schema objects are equally plausible."""
    schema_tables = ["account1", "account2"]
    match, is_unambiguous = find_unambiguous_schema_match("account", schema_tables)
    # Both account1 and account2 have equal similarity; do not guess!
    assert is_unambiguous is False
    assert match is None


def test_pre_presentation_correction_loop(sample_state):
    """Verify generator repairs recoverable unknown-table typo before presenting draft."""
    mock_llm = MagicMock()
    # First response: typo table 'user'
    # Second response: corrected table 'users'
    mock_llm.generate.side_effect = [
        GenerationResponse(text="SELECT id, username FROM user LIMIT 5;", provider="ollama", model="qwen"),
        GenerationResponse(text="SELECT id, username FROM users LIMIT 5;", provider="ollama", model="qwen"),
    ]

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show 5 users", sample_state)

    assert draft.is_validated is True
    assert draft.is_schema_verified is True
    assert "users" in draft.sql
    assert draft.repair_attempts == 1
    assert "Schema-grounded correction applied" in (draft.correction_notes or "")


def test_post_execution_recovery_offers_fresh_approval(sample_state):
    """Verify execution failure produces a corrected draft requiring fresh explicit approval."""
    draft = QueryDraft(
        question="Count user orders",
        sql="SELECT user_id, COUNT(*) FROM orders;",
        explanation="Counts orders.",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
        repair_attempts=0,
    )
    sample_state.last_query_draft = draft

    # First execution fails due to missing GROUP BY in PostgreSQL
    failed_result = QueryResult(
        executed_sql=draft.sql,
        columns=[],
        rows=[],
        row_count=0,
        duration_ms=10.0,
        dialect="postgres",
        database="testdb",
        error_message='column "orders.user_id" must appear in the GROUP BY clause or be used in an aggregate function',
    )

    mock_executor = MagicMock()
    mock_executor.execute.side_effect = [
        failed_result,
        # Corrected execution result
        QueryResult(
            executed_sql="SELECT user_id, COUNT(*) FROM orders GROUP BY user_id;",
            columns=["user_id", "count"],
            rows=[(1, 5)],
            row_count=1,
            duration_ms=11.0,
            dialect="postgres",
            database="testdb",
        ),
    ]

    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="SELECT user_id, COUNT(*) FROM orders GROUP BY user_id;",
        provider="ollama",
        model="qwen",
    )

    # User approves initial execution ('y') and then approves corrected execution ('y')
    user_inputs = ["y", "y"]
    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        llm_service=mock_llm,
        user_input_func=lambda _: user_inputs.pop(0),
        executor=mock_executor,
    )

    assert mock_executor.execute.call_count == 2
    assert sample_state.last_query_result is not None
    assert sample_state.last_query_result.is_success is True


def test_non_recoverable_errors_do_not_trigger_llm_repair(sample_state):
    """Verify connection timeouts and permission errors do not trigger repeated LLM requests."""
    draft = QueryDraft(
        question="List orders",
        sql="SELECT * FROM orders LIMIT 5;",
        explanation="Orders",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    sample_state.last_query_draft = draft

    # Simulate timeout failure
    timeout_result = QueryResult(
        executed_sql=draft.sql,
        columns=[],
        rows=[],
        row_count=0,
        duration_ms=10005.0,
        dialect="postgres",
        database="testdb",
        error_message="Query execution timed out after 10.0 seconds",
    )

    mock_executor = MagicMock()
    mock_executor.execute.return_value = timeout_result
    mock_llm = MagicMock()

    handle_conversational_execution(
        draft=draft,
        state=sample_state,
        llm_service=mock_llm,
        user_input_func=lambda _: "y",
        executor=mock_executor,
    )

    # LLM should NOT be invoked to rewrite query for timeout!
    mock_llm.generate.assert_not_called()
    assert is_recoverable_execution_error(timeout_result.error_message) is False


def test_stale_draft_invalidation_on_database_change(sample_state):
    """Verify switching active database clears cached schema and invalidates prior draft execution."""
    draft = QueryDraft(
        question="List orders",
        sql="SELECT * FROM orders LIMIT 5;",
        explanation="Orders",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
    )
    sample_state.last_query_draft = draft
    sample_state.last_query_result = MagicMock()

    # Change database
    sample_state.set_database(None)
    assert sample_state.cached_schema is None
    assert sample_state.last_query_result is None

    # Running with no active database is rejected
    executed = handle_run_command(state=sample_state, user_input_func=lambda _: "y")
    assert executed is False


def test_try_expand_wildcard_projections_ignores_mutations(mock_schema):
    """Verify try_expand_wildcard_projections does not touch UPDATE statements or queries without Star."""
    import sqlglot
    from support.query.privacy import try_expand_wildcard_projections

    # UPDATE statement should never be altered
    update_ast = sqlglot.parse_one("UPDATE users SET email = 'arjun@example.com' WHERE username = 'arjun'", read="postgres")
    res = try_expand_wildcard_projections(update_ast, mock_schema, "postgres")
    assert res is None

    # SELECT with explicit columns should not be altered
    select_explicit = sqlglot.parse_one("SELECT username, email FROM users", read="postgres")
    res_sel = try_expand_wildcard_projections(select_explicit, mock_schema, "postgres")
    assert res_sel is None

    # SELECT * with sensitive columns SHOULD be safely expanded
    select_star = sqlglot.parse_one("SELECT * FROM users", read="postgres")
    res_star = try_expand_wildcard_projections(select_star, mock_schema, "postgres")
    assert res_star is not None
    assert "password_hash" not in res_star.sql("postgres")


def test_conversational_execution_mutation_failure_invokes_llm_repair(sample_state):
    """Verify that when an UPDATE execution fails, LLM is called to repair it and execute the fix."""
    from support.llm.models import GenerationResponse

    draft = QueryDraft(
        question="change Arjun's email to arjun.reddy@example.com",
        sql="UPDATE users SET id, username, email WHERE username = 'Arjun'",
        explanation="Update email",
        dialect="postgres",
        validation_status="Passed",
        is_validated=True,
        is_schema_verified=True,
        operation="UPDATE",
        is_readonly=False,
    )
    sample_state.last_query_draft = draft

    mock_llm = MagicMock()
    # LLM returns the repaired UPDATE statement
    mock_llm.generate.return_value = GenerationResponse(
        text=(
            "```sql\n"
            "UPDATE users SET email = 'arjun.reddy@example.com' WHERE username = 'Arjun';\n"
            "```\n"
            "Explanation: Corrected syntax to assign email column properly."
        ),
        provider="test-prov",
        model="test-model",
    )

    prompts_asked = []

    def mock_input(prompt):
        prompts_asked.append(prompt)
        return "y"

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.connect.return_value.__enter__.return_value = mock_conn

    # First execution fails with psycopg syntax error, repair execution succeeds
    with patch("support.db.connection.DatabaseConnectionFactory.create_engine") as mock_create_engine:
        # First call fails on execute, second call succeeds
        call_count = 0

        def fake_execute(stmt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("(psycopg.errors.SyntaxError) syntax error at or near \",\"\nLINE 1: UPDATE users SET id, username, email WHERE...")
            res = MagicMock()
            res.rowcount = 1
            return res

        mock_conn.execute.side_effect = fake_execute
        mock_create_engine.return_value = mock_engine

        result = handle_conversational_execution(
            draft=draft,
            state=sample_state,
            llm_service=mock_llm,
            user_input_func=mock_input,
        )

        assert result is False  # Original failed
        assert mock_llm.generate.called  # LLM was invoked to fix the failure!
        assert len(prompts_asked) == 2  # 1st approval for flawed query, 2nd approval for repaired query
        assert "corrected UPDATE" in prompts_asked[1]
        assert sample_state.last_query_draft.sql.rstrip(";") == "UPDATE users SET email = 'arjun.reddy@example.com' WHERE username = 'Arjun'"
        assert "Executed (UPDATE: 1 rows" in sample_state.last_query_draft.execution_status

