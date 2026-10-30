"""Tests for natural-language multi-table inspection workflow."""

from unittest.mock import MagicMock, patch
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.history.store import QueryHistoryStore
from support.query.executor import ExecutionConfig, QueryResult, ReadOnlyQueryExecutor
from support.query.inspection import (
    MultiTableInspectionPlan,
    TableInspectionItem,
    build_inspection_plan,
    compute_plan_hash,
    execute_multi_table_inspection,
)
from support.tools.selector import interpret_and_select_tool
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.workspace.session import run_workspace_session
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def sample_inspection_schema():
    """Create a test relational schema with safe columns, sensitive columns, and an empty table."""
    # 1. users: has sensitive columns (password_hash, api_key) and safe columns (id, username, created_at)
    users_tbl = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR(50)"),
            ColumnSchema(name="password_hash", data_type="VARCHAR(255)"),
            ColumnSchema(name="api_key", data_type="VARCHAR(64)"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
    )

    # 2. tasks: all safe columns
    tasks_tbl = TableSchema(
        name="tasks",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="title", data_type="VARCHAR(100)"),
            ColumnSchema(name="status", data_type="VARCHAR(20)"),
            ColumnSchema(name="user_id", data_type="INTEGER"),
        ],
    )

    # 3. secrets: ONLY sensitive columns (no safe columns)
    secrets_tbl = TableSchema(
        name="secrets",
        columns=[
            ColumnSchema(name="token", data_type="VARCHAR(255)"),
            ColumnSchema(name="private_key", data_type="TEXT"),
            ColumnSchema(name="salt", data_type="VARCHAR(32)"),
        ],
    )

    return DatabaseSchema(
        database_name="test_db",
        dialect=Dialect.POSTGRES,
        tables={"users": users_tbl, "tasks": tasks_tbl, "secrets": secrets_tbl},
    )


@pytest.fixture
def sample_profile():
    return ConnectionProfile(
        name="athena",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="test_db",
        username="postgres",
    )


def test_tool_selection_multi_table_vs_exploration_and_queries(sample_inspection_schema, sample_profile):
    """Verify tool selector distinguishes inspection from exploration, single queries, and mutations."""
    state = WorkspaceSessionState(active_database="athena")
    state.cached_schema = sample_inspection_schema

    # 1. Multi-table inspection requests (all tables)
    res1 = interpret_and_select_tool("Display all tables and print the first row from each table.", state=state, profile=sample_profile)
    assert res1.tool == "inspect_tables"
    assert res1.arguments.get("tables") == []
    assert res1.arguments.get("row_limit") == 1

    res2 = interpret_and_select_tool("Give me a quick overview of the data in every table.", state=state, profile=sample_profile)
    assert res2.tool == "inspect_tables"
    assert res2.arguments.get("row_limit") == 1

    # 2. Multi-table inspection requests (specific tables + custom row limit)
    res3 = interpret_and_select_tool("Show the first three records from users and tasks.", state=state, profile=sample_profile)
    assert res3.tool == "inspect_tables"
    assert set(res3.arguments.get("tables", [])) == {"users", "tasks"}
    assert res3.arguments.get("row_limit") == 3

    res4 = interpret_and_select_tool("Show 5 rows from users and tasks", state=state, profile=sample_profile)
    assert res4.tool == "inspect_tables"
    assert res4.arguments.get("row_limit") == 5

    # 3. Schema exploration only (no row data requested)
    res_expl1 = interpret_and_select_tool("Show all tables.", state=state, profile=sample_profile)
    assert res_expl1.tool in ("list_tables", "inspect_tables")

    res_expl2 = interpret_and_select_tool("List tables", state=state, profile=sample_profile)
    assert res_expl2.tool == "list_tables"

    # 4. Single-table query (handled by ordinary single-query generator)
    res_single = interpret_and_select_tool("Show the first three records from users", state=state, profile=sample_profile)
    assert res_single.tool == "query_database"

    # 5. Data mutation (routed to mutation tools)
    res_mut = interpret_and_select_tool("Add a new record to users", state=state, profile=sample_profile)
    assert res_mut.tool in ("create_record", "update_records")


def test_inspection_plan_excludes_sensitive_columns(sample_profile, sample_inspection_schema):
    """Verify build_inspection_plan projects only safe columns and excludes credentials."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["users"],
        row_limit=1,
    )
    assert plan is not None
    assert len(plan.eligible_tables) == 1
    user_item = plan.eligible_tables[0]

    # Safe columns included
    assert "id" in user_item.columns
    assert "username" in user_item.columns
    assert "created_at" in user_item.columns

    # Sensitive columns excluded
    assert "password_hash" not in user_item.columns
    assert "api_key" not in user_item.columns
    assert "password_hash" in user_item.sensitive_columns_excluded
    assert "api_key" in user_item.sensitive_columns_excluded

    # SQL uses explicit column list
    assert 'SELECT "id", "username", "created_at" FROM "users" LIMIT 1;' == user_item.sql


def test_inspection_plan_skips_table_with_no_safe_columns(sample_profile, sample_inspection_schema):
    """Verify tables containing only sensitive columns are skipped with clear explanation."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["secrets"],
        row_limit=1,
    )
    assert plan is not None
    assert len(plan.eligible_tables) == 0
    assert len(plan.skipped_tables) == 1
    skipped = plan.skipped_tables[0]
    assert skipped.table_name == "secrets"
    assert skipped.is_skipped is True
    assert "sensitive column(s)" in skipped.skip_reason


def test_inspection_plan_separate_queries_all_tables(sample_profile, sample_inspection_schema):
    """Verify each eligible table receives a distinct single SELECT statement."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=None,  # all tables
        row_limit=2,
    )
    assert plan is not None
    # users and tasks eligible, secrets skipped
    eligible_names = [t.table_name for t in plan.eligible_tables]
    assert eligible_names == ["tasks", "users"]

    for item in plan.eligible_tables:
        assert item.sql is not None
        assert "LIMIT 2;" in item.sql
        # Never multiple statements in one query
        assert item.sql.count(";") == 1


def test_inspection_plan_dialect_specific_limiting(sample_inspection_schema):
    """Verify SQLGlot limits queries according to dialect (LIMIT vs TOP)."""
    # MSSQL test
    mssql_prof = ConnectionProfile(name="ms_db", dialect=Dialect.MSSQL, database="test")
    mssql_plan = build_inspection_plan(
        profile=mssql_prof,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    assert mssql_plan is not None
    assert "TOP 1" in mssql_plan.eligible_tables[0].sql
    assert "[tasks]" in mssql_plan.eligible_tables[0].sql

    # MySQL test
    mysql_prof = ConnectionProfile(name="my_db", dialect=Dialect.MYSQL, database="test")
    mysql_plan = build_inspection_plan(
        profile=mysql_prof,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    assert mysql_plan is not None
    assert "LIMIT 1;" in mysql_plan.eligible_tables[0].sql
    assert "`tasks`" in mysql_plan.eligible_tables[0].sql


def test_inspection_plan_table_cap_workflow(sample_profile):
    """Verify configurable table cap asks user instead of silently omitting tables."""
    # Generate schema with 25 tables
    tables = {
        f"table_{i:02d}": TableSchema(name=f"table_{i:02d}", columns=[ColumnSchema(name="id", data_type="INT")])
        for i in range(25)
    }
    big_schema = DatabaseSchema(database_name="big", dialect=Dialect.POSTGRES, tables=tables)

    # 1. User declines cap -> plan returns None
    plan_declined = build_inspection_plan(
        profile=sample_profile,
        schema=big_schema,
        target_tables=None,
        max_tables=10,
        user_input_func=lambda prompt: "n",
    )
    assert plan_declined is None

    # 2. User confirms cap -> plan has first 10 tables with is_capped=True
    plan_accepted = build_inspection_plan(
        profile=sample_profile,
        schema=big_schema,
        target_tables=None,
        max_tables=10,
        user_input_func=lambda prompt: "y",
    )
    assert plan_accepted is not None
    assert plan_accepted.is_capped is True
    assert len(plan_accepted.tables) == 10
    assert plan_accepted.total_database_tables == 25


def test_inspection_approval_and_cancellation(sample_profile, sample_inspection_schema):
    """Verify execution requires explicit approval with Enter defaulting to cancel."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    state = WorkspaceSessionState(active_database="athena")
    mock_executor = MagicMock(spec=ReadOnlyQueryExecutor)

    # 1. User presses Enter (defaulting to No)
    res_cancel = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "",  # empty input = No
    )
    assert res_cancel is None
    mock_executor.execute.assert_not_called()

    # 2. User approves ("y")
    mock_executor.execute.return_value = QueryResult(
        columns=["id", "title", "status", "user_id"],
        rows=[[1, "Deploy app", "done", 10]],
        row_count=1,
        duration_ms=15.0,
    )
    res_approved = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "y",
    )
    assert res_approved is not None
    assert res_approved.total_inspected == 1
    assert res_approved.tables_with_rows == 1
    assert mock_executor.execute.call_count == 1


def test_inspection_plan_immutability_tampering_check(sample_profile, sample_inspection_schema):
    """Verify execution aborts if plan SQL is modified after approval hash was generated."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    state = WorkspaceSessionState(active_database="athena")
    mock_executor = MagicMock(spec=ReadOnlyQueryExecutor)

    # Tamper with the plan SQL
    plan.tables[0].sql = "SELECT id, title FROM tasks WHERE 1=1; DROP TABLE tasks;"

    res = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "y",
    )
    assert res is None
    mock_executor.execute.assert_not_called()


def test_inspection_partial_failure_continues_remaining_tables(sample_profile, sample_inspection_schema):
    """Verify a failure on one table does not prevent inspecting the remaining tables."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["tasks", "users"],
        row_limit=1,
    )
    state = WorkspaceSessionState(active_database="athena")
    mock_executor = MagicMock(spec=ReadOnlyQueryExecutor)

    # tasks fails, users succeeds
    fail_res = QueryResult(
        columns=[],
        rows=[],
        row_count=0,
        error_message="Permission denied on relation tasks",
        duration_ms=10.0,
    )
    success_res = QueryResult(
        columns=["id", "username", "created_at"],
        rows=[[1, "alice", "2026-01-01"]],
        row_count=1,
        duration_ms=12.0,
    )
    mock_executor.execute.side_effect = [fail_res, success_res]

    res = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "y",
    )
    assert res is not None
    # Both tables were attempted
    assert mock_executor.execute.call_count == 2
    assert res.total_inspected == 2
    assert res.failed_tables == 1
    assert res.tables_with_rows == 1
    assert res.table_results[0].is_error is True
    assert res.table_results[1].query_result.is_success is True


def test_inspection_stale_database_rejected(sample_profile, sample_inspection_schema):
    """Verify plan generated for database A cannot be executed when active database is switched to B."""
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    # Active database changed to "other_db"
    state = WorkspaceSessionState(active_database="other_db")
    mock_executor = MagicMock(spec=ReadOnlyQueryExecutor)

    res = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "y",
    )
    assert res is None
    mock_executor.execute.assert_not_called()


def test_inspection_history_integration_without_result_rows(tmp_path, sample_profile, sample_inspection_schema):
    """Verify query history records inspection metadata but NEVER stores result rows."""
    store = QueryHistoryStore(config_dir=tmp_path)
    plan = build_inspection_plan(
        profile=sample_profile,
        schema=sample_inspection_schema,
        target_tables=["tasks"],
        row_limit=1,
    )
    state = WorkspaceSessionState(active_database="athena")
    mock_executor = MagicMock(spec=ReadOnlyQueryExecutor)
    mock_executor.execute.return_value = QueryResult(
        columns=["id", "title"],
        rows=[[101, "Confidential Project Name"]],
        row_count=1,
        duration_ms=8.0,
    )

    res = execute_multi_table_inspection(
        plan=plan,
        profile=sample_profile,
        state=state,
        executor=mock_executor,
        user_input_func=lambda p: "y",
        store=store,
        original_question="Overview of tasks",
    )
    assert res is not None

    entries = store.list_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entry_type == "multi_table_inspection"
    assert entry.database == "athena"
    assert entry.row_count == 1
    assert "Inspected 1 table" in entry.execution_status

    # Verify result rows are NEVER written to disk
    history_file_content = (tmp_path / "query_history.json").read_text(encoding="utf-8")
    assert "Confidential Project Name" not in history_file_content


def test_multi_table_inspection_mongodb_boundary():
    """Verify MongoDB rejects multi-table inspection cleanly."""
    mongo_prof = ConnectionProfile(name="mongo_app", dialect=Dialect.MONGODB, database="appdb")
    schema = DatabaseSchema(database_name="appdb", dialect=Dialect.MONGODB, tables={})
    plan = build_inspection_plan(profile=mongo_prof, schema=schema)
    assert plan is None


def test_inspection_workspace_session_end_to_end(sample_profile, sample_inspection_schema):
    """Verify end-to-end workspace session routes to inspection and never invokes LLM."""
    from support.db.service import DatabaseService
    from support.llm.service import LLMService

    state = WorkspaceSessionState(active_database="athena", database_profile=sample_profile)
    state.cached_schema = sample_inspection_schema

    inputs = [
        "Display all tables and print the first row from each table.",
        "y",  # Approve preview
        "/exit",
    ]

    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (sample_profile, None)
    mock_llm = MagicMock()

    with patch.object(WorkspaceSessionState, "initialize", return_value=state), \
         patch("support.query.inspection.ReadOnlyQueryExecutor.execute") as mock_exec, \
         patch("support.query.generator.QueryGenerator.generate_draft") as mock_gen:

        mock_exec.return_value = QueryResult(
            columns=["id", "name"],
            rows=[[1, "sample"]],
            row_count=1,
            duration_ms=5.0,
        )

        run_workspace_session(
            db_service=mock_db,
            llm_service=mock_llm,
            input_func=lambda p: inputs.pop(0),
        )

        # Inspection executed queries for eligible tables
        assert mock_exec.call_count == 2  # tasks and users (secrets was skipped)
        # LLM query generator was NEVER called!
        mock_gen.assert_not_called()
        # State recorded the completion message
        assert any("Multi-table inspection complete" in m.content for m in state.history)

