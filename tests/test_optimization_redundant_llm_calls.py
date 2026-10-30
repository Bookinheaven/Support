"""
Regression tests verifying elimination of redundant tool-selection LLM calls
while preserving SQL correctness, dialect safety, approvals, and repair workflows.
"""

from unittest.mock import MagicMock, patch
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.llm.models import GenerationResponse, GenerationUsage
from support.llm.service import LLMService
from support.query.generator import QueryGenerator
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.workspace.session import run_workspace_session
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def mock_schema():
    users_tbl = TableSchema(
        name="users",
        schema_name="public",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="email", data_type="VARCHAR(255)"),
            ColumnSchema(name="full_name", data_type="VARCHAR(100)"),
        ],
    )
    products_tbl = TableSchema(
        name="products",
        schema_name="public",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="name", data_type="VARCHAR(100)"),
            ColumnSchema(name="price", data_type="NUMERIC(10, 2)"),
            ColumnSchema(name="stock", data_type="INTEGER"),
        ],
    )
    return DatabaseSchema(
        database_name="support_dev",
        tables={"public.users": users_tbl, "public.products": products_tbl},
    )


@pytest.fixture
def pg_profile():
    return ConnectionProfile(
        name="dev-pg",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5435,
        database="support_dev",
        username="support_user",
    )


# ---------------------------------------------------------------------------
# Test 1 & 2: Natural language SELECT does not make redundant tool-selection LLM calls
# ---------------------------------------------------------------------------
def test_select_request_does_not_invoke_redundant_tool_selection(pg_profile, mock_schema):
    """Verify natural-language SELECT makes exactly 1 LLM call (sql_generation) and 0 tool calls."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT id, email, full_name FROM users LIMIT 5;\n```\nExplanation: First 5 users",
        provider="ollama",
        model="qwen2.5-coder:7b",
        usage=GenerationUsage(prompt_tokens=120, completion_tokens=25, total_tokens=145),
        latency_ms=10.0,
    )

    inputs = ["Show me the first five rows from the users table.", "n", "/exit"]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))

    # Exactly 1 LLM generation call, 0 tool-selection calls
    assert mock_llm.generate.call_count == 1
    metrics = state.last_performance_metrics.to_dict()
    assert metrics["llm_calls_count"] == 1
    assert metrics["tool_selection_calls_count"] == 0
    assert metrics["sql_generation_calls_count"] == 1
    assert state.last_query_draft is not None
    assert "SELECT" in state.last_query_draft.sql.upper()


# ---------------------------------------------------------------------------
# Test 3: CREATE TABLE requests generate DDL rather than SELECT
# ---------------------------------------------------------------------------
def test_create_table_generates_ddl_and_requires_approval(pg_profile, mock_schema):
    """Verify CREATE TABLE generates DDL, is not routed to SELECT-only, and enforces approval."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    ddl_sql = "CREATE TABLE orders (\n  id SERIAL PRIMARY KEY,\n  total NUMERIC(10, 2)\n);"
    mock_llm.generate.return_value = GenerationResponse(
        text=f"```sql\n{ddl_sql}\n```\nExplanation: Creates orders table",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    inputs = [
        "Create an orders table with id and total",
        "n",  # Decline execution to verify safety approval safeguard
        "/exit",
    ]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))

    draft = state.last_query_draft
    assert draft is not None
    assert "CREATE TABLE" in draft.sql.upper()
    assert draft.operation == "CREATE TABLE"
    assert not draft.is_readonly
    # Safe rejection when user says 'n'
    assert draft.execution_status == "Not performed" or "Draft" in draft.execution_status


# ---------------------------------------------------------------------------
# Test 4: ALTER TABLE requests retain their intended operation
# ---------------------------------------------------------------------------
def test_alter_table_generates_alter_operation(pg_profile, mock_schema):
    """Verify ALTER TABLE preserves operation type and approval safeguards."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    alter_sql = "ALTER TABLE products ADD COLUMN category VARCHAR(50);"
    mock_llm.generate.return_value = GenerationResponse(
        text=f"```sql\n{alter_sql}\n```\nExplanation: Adds category column to products",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    inputs = ["Add a category column to products", "n", "/exit"]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))

    draft = state.last_query_draft
    assert draft is not None
    assert "ALTER TABLE" in draft.sql.upper()
    assert draft.operation == "ALTER TABLE"
    assert not draft.is_readonly


# ---------------------------------------------------------------------------
# Test 5: INSERT, UPDATE, DELETE maintain safety and approval workflow
# ---------------------------------------------------------------------------
def test_mutation_requests_maintain_safety_and_approval(pg_profile, mock_schema):
    """Verify UPDATE requires confirmation and operates via QueryGenerator."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    update_sql = "UPDATE products SET stock = 50 WHERE id = 1;"
    mock_llm.generate.return_value = GenerationResponse(
        text=f"```sql\n{update_sql}\n```\nExplanation: Update stock for product 1",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    inputs = ["Update the stock of product 1 to 50", "n", "/exit"]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))

    draft = state.last_query_draft
    assert draft is not None
    assert "UPDATE" in draft.sql.upper()
    assert draft.operation == "UPDATE"
    assert not draft.is_readonly
    # Exactly 1 LLM call was made
    assert mock_llm.generate.call_count == 1


# ---------------------------------------------------------------------------
# Test 6: Schema inspection and ER diagram requests use deterministic paths
# ---------------------------------------------------------------------------
def test_schema_inspection_and_er_diagram_deterministic(pg_profile, mock_schema):
    """Verify schema listing and ER diagram use deterministic capabilities without LLM calls."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)

    inputs = [
        "show all tables",  # deterministic list_tables
        "generate ER diagram",  # deterministic generate_diagram
        "/exit",
    ]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        with patch("support.tools.executor.handle_schema") as mock_hs:
            with patch("support.tools.executor.handle_diagram") as mock_hd:
                run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))
                assert mock_hs.called
                assert mock_hd.called

    # 0 LLM calls were required for deterministic schema and diagram operations
    assert mock_llm.generate.call_count == 0


# ---------------------------------------------------------------------------
# Test 7: Multi-turn follow-up questions retain context
# ---------------------------------------------------------------------------
def test_multi_turn_followup_retains_context(pg_profile, mock_schema):
    """Verify follow-up question prompt contains prior conversation history."""
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.side_effect = [
        GenerationResponse(
            text="```sql\nSELECT id, full_name, email FROM users LIMIT 10;\n```\nExplanation: First 10 users",
            provider="ollama",
            model="qwen2.5-coder:7b",
        ),
        GenerationResponse(
            text="```sql\nSELECT full_name, email FROM users LIMIT 10;\n```\nExplanation: Names and emails only",
            provider="ollama",
            model="qwen2.5-coder:7b",
        ),
    ]

    inputs = [
        "Show me the first ten users.",
        "n",  # Decline execution for turn 1
        "Now show only their names and email addresses.",
        "n",  # Decline execution for turn 2
        "/exit",
    ]

    with patch.object(WorkspaceSessionState, "initialize", return_value=state):
        run_workspace_session(db_service=mock_db, llm_service=mock_llm, input_func=lambda p: inputs.pop(0))

    assert mock_llm.generate.call_count == 2
    # Verify the second call's prompt contained the prior question and draft
    second_prompt = mock_llm.generate.call_args_list[1][1].get("prompt", "")
    assert "Show me the first ten users." in second_prompt


# ---------------------------------------------------------------------------
# Test 8: Invalid SQL recovery triggers repair attempt
# ---------------------------------------------------------------------------
def test_invalid_sql_triggers_repair_attempt(pg_profile, mock_schema):
    """Verify that when initial SQL fails validation, repair is attempted and tracked."""
    generator = QueryGenerator(llm_service=MagicMock())
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=pg_profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = mock_schema

    # First call generates bad syntax; second call repairs it
    generator.llm_service.generate.side_effect = [
        GenerationResponse(
            text="```sql\nSELECT * FORM users;\n```\nExplanation: Syntax typo",
            provider="ollama",
            model="qwen2.5-coder:7b",
        ),
        GenerationResponse(
            text="```sql\nSELECT id, email, full_name FROM users;\n```\nExplanation: Fixed typo",
            provider="ollama",
            model="qwen2.5-coder:7b",
        ),
    ]

    from support.core.performance import PerformanceTracker, set_current_tracker

    tracker = PerformanceTracker("repair_test")
    set_current_tracker(tracker)
    try:
        draft = generator.generate_draft("Show users", state)
    finally:
        set_current_tracker(None)

    assert generator.llm_service.generate.call_count == 2
    assert tracker.repair_attempts_count == 1
    assert tracker.repair_calls_count == 1
    assert draft.is_validated
    assert "FORM" not in draft.sql


# ---------------------------------------------------------------------------
# Test 9: Performance metrics accurately count LLM calls and breakdown
# ---------------------------------------------------------------------------
def test_performance_metrics_accurate_call_counting():
    """Verify PerformanceTracker accurately separates tool, sql, and repair calls."""
    from support.core.performance import PerformanceTracker

    tracker = PerformanceTracker("metrics_test")
    tracker.record_llm_call(purpose="tool_selection", duration_ms=100.0, provider="ollama", model="test")
    tracker.record_llm_call(purpose="sql_generation", duration_ms=250.0, provider="ollama", model="test")
    tracker.record_llm_call(purpose="sql_repair", duration_ms=200.0, provider="ollama", model="test")

    d = tracker.to_dict()
    assert d["llm_calls_count"] == 3
    assert d["tool_selection_calls_count"] == 1
    assert d["sql_generation_calls_count"] == 1
    assert d["repair_calls_count"] == 1
    assert len(d["llm_calls"]) == 3
