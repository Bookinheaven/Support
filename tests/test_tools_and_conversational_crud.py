"""Tests for direct tool-based interaction, conversational CRUD, and sample data generation."""

from unittest.mock import MagicMock, patch
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.history.store import QueryHistoryStore
from support.mutation.executor import DataModificationExecutor
from support.mutation.models import MutationType, PendingMutation
from support.mutation.planner import resolve_mutation_target_table
from support.mutation.sample_data import (
    generate_plausible_field_value,
    generate_secure_test_password,
    hash_password,
    populate_sample_and_derived_values,
)
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.tools.executor import execute_database_tool, resume_pending_mutation
from support.tools.models import ToolCall
from support.tools.registry import DATABASE_TOOLS, get_available_tools
from support.tools.selector import _is_sample_data_authorized, select_tool_for_user_request
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def mock_users_schema():
    """Schema mirroring the live PostgreSQL users table."""
    users_tbl = TableSchema(
        name="users",
        schema_name="public",
        columns=[
            ColumnSchema(name="id", data_type="UUID", is_primary_key=True, default_value="gen_random_uuid()"),
            ColumnSchema(name="username", data_type="VARCHAR(20)", nullable=False),
            ColumnSchema(name="username_lower", data_type="VARCHAR(20)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(255)", nullable=False),
            ColumnSchema(name="password_hash", data_type="VARCHAR(255)", nullable=False),
            ColumnSchema(name="account_type", data_type="VARCHAR(20)", nullable=False, default_value="'user'"),
            ColumnSchema(name="full_name", data_type="VARCHAR(50)", nullable=False),
            ColumnSchema(name="is_active", data_type="BOOLEAN", nullable=False, default_value="true"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False, default_value="now()"),
        ],
        primary_key=["id"],
    )
    tasks_tbl = TableSchema(
        name="tasks",
        schema_name="public",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="title", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="status", data_type="VARCHAR(20)", nullable=False, default_value="'pending'"),
        ],
        primary_key=["id"],
    )
    return DatabaseSchema(
        database_name="athena",
        dialect=Dialect.POSTGRES,
        tables={"public.users": users_tbl, "public.tasks": tasks_tbl},
    )


@pytest.fixture
def sample_profile():
    return ConnectionProfile(
        name="athena",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5433,
        database="athena",
        username="postgres",
    )


@pytest.fixture
def session_state(sample_profile, mock_users_schema):
    state = WorkspaceSessionState(active_database="athena", database_profile=sample_profile)
    state.cached_schema = mock_users_schema
    return state


# -------------------------------------------------------------------------
# 1. Tool Registry & Definitions
# -------------------------------------------------------------------------
def test_all_expected_tools_are_registered():
    """Verify all 8+ required database tools are registered with schemas."""
    tools = get_available_tools()
    tool_names = {t.name for t in tools}
    expected = {
        "list_tables",
        "inspect_tables",
        "generate_diagram",
        "query_database",
        "create_record",
        "update_records",
        "delete_records",
        "explain_error",
        "ask_clarification",
    }
    assert expected.issubset(tool_names)
    for t in tools:
        assert t.description
        assert "type" in t.parameters


# -------------------------------------------------------------------------
# 2. Sample Data Authorization Detection
# -------------------------------------------------------------------------
def test_sample_data_authorization_detection():
    """Verify natural-language authorization of sample or random data is recognized."""
    authorized_phrases = [
        "i want to add a user to users table, the details can be random",
        "Add a user to users table; the details can be random.",
        "Create a test user with random details.",
        "Insert a sample user into users.",
        "Generate dummy data for a user record.",
        "add a user with random details",
        "create a dummy record in users",
    ]
    for phrase in authorized_phrases:
        assert _is_sample_data_authorized(phrase) is True, f"Failed for: {phrase}"

    unauthorized_phrases = [
        "add a user in users table",
        "insert a record into users",
        "add user with username alex and email alex@example.com",
        "show all users",
        "delete a record from users where id = 5",
    ]
    for phrase in unauthorized_phrases:
        assert _is_sample_data_authorized(phrase) is False, f"Should be false for: {phrase}"


# -------------------------------------------------------------------------
# 3. Direct Tool Selection
# -------------------------------------------------------------------------
def test_tool_selection_for_sample_data_insert(mock_users_schema, sample_profile, session_state):
    """Verify user request with random details selects create_record with allow_sample_data=True."""
    prompt = "i want to add a user to users table, the details can be random"
    call = select_tool_for_user_request(
        message=prompt,
        schema=mock_users_schema,
        profile=sample_profile,
        state=session_state,
    )
    assert call.tool == "create_record"
    assert call.arguments.get("table_name") == "users"
    assert call.arguments.get("allow_sample_data") is True


def test_tool_selection_for_explicit_crud_operations(mock_users_schema, sample_profile, session_state):
    """Verify tool selector maps CRUD requests accurately."""
    # Insert without sample data
    call_insert = select_tool_for_user_request("add a user in users table", mock_users_schema, sample_profile, session_state)
    assert call_insert.tool == "create_record"
    assert call_insert.arguments.get("table_name") == "users"
    assert call_insert.arguments.get("allow_sample_data") is False

    # Update
    call_update = select_tool_for_user_request("update the users table set is_active = false where id = 5", mock_users_schema, sample_profile, session_state)
    assert call_update.tool == "update_records"
    assert call_update.arguments.get("table_name") == "users"

    # Delete
    call_del = select_tool_for_user_request("delete a record from users where id = 10", mock_users_schema, sample_profile, session_state)
    assert call_del.tool == "delete_records"
    assert call_del.arguments.get("table_name") == "users"

    # Read-only query
    call_query = select_tool_for_user_request("show the top 10 users by created date", mock_users_schema, sample_profile, session_state)
    assert call_query.tool == "query_database"

    call_query2 = select_tool_for_user_request("show everything in orders items table", mock_users_schema, sample_profile, session_state)
    assert call_query2.tool == "query_database"

    # List tables
    call_list = select_tool_for_user_request("show all tables", mock_users_schema, sample_profile, session_state)
    assert call_list.tool == "list_tables"

    # Inspect tables
    call_insp = select_tool_for_user_request("display all tables and print first row of each table", mock_users_schema, sample_profile, session_state)
    assert call_insp.tool == "inspect_tables"
    assert call_insp.arguments.get("all_tables") is True

    # ER diagram
    call_diag = select_tool_for_user_request("generate ER diagram", mock_users_schema, sample_profile, session_state)
    assert call_diag.tool == "generate_diagram"


# -------------------------------------------------------------------------
# 4. Sample and Derived Values Generation
# -------------------------------------------------------------------------
def test_populate_sample_values_when_authorized(mock_users_schema):
    """Verify required fields are populated with plausible values, username_lower derived, and password hashed."""
    users_tbl = mock_users_schema.get_table("users")
    values, missing = populate_sample_and_derived_values(
        table=users_tbl,
        schema=mock_users_schema,
        initial_values={},
        allow_sample_data=True,
    )
    assert len(missing) == 0
    assert "username" in values
    assert "username_lower" in values
    assert values["username_lower"] == values["username"].lower()
    assert "email" in values
    assert "@" in values["email"]
    assert "password_hash" in values
    assert values["password_hash"].startswith("$2b$12$")
    assert "full_name" in values
    assert len(values["full_name"].split()) >= 2


def test_username_lower_derived_from_user_supplied_username(mock_users_schema):
    """Verify username_lower is derived deterministically when user provides username."""
    users_tbl = mock_users_schema.get_table("users")
    values, missing = populate_sample_and_derived_values(
        table=users_tbl,
        schema=mock_users_schema,
        initial_values={"username": "AliceSmith"},
        allow_sample_data=False,
    )
    assert values["username_lower"] == "alicesmith"
    # Other unprovided required columns remain missing when allow_sample_data is False
    assert "email" in missing
    assert "password" in missing
    assert "full_name" in missing


def test_password_hash_generated_from_provided_password(mock_users_schema):
    """Verify plaintext password is securely hashed and never persisted directly."""
    users_tbl = mock_users_schema.get_table("users")
    initial = {"username": "bob", "password": "MySuperSecretPassword123!"}
    hashed = hash_password(initial.pop("password"))
    initial["password_hash"] = hashed

    values, missing = populate_sample_and_derived_values(
        table=users_tbl,
        schema=mock_users_schema,
        initial_values=initial,
        allow_sample_data=False,
    )
    assert "password" not in values
    assert values["password_hash"].startswith("$2b$12$")
    assert values["username_lower"] == "bob"


# -------------------------------------------------------------------------
# 5. Conversational Follow-up and Pending Mutation Resumption
# -------------------------------------------------------------------------
def test_missing_required_fields_saves_pending_mutation(session_state):
    """Verify operation without sample data or required fields prompts in natural language and saves pending mutation."""
    call = ToolCall(tool="create_record", arguments={"table_name": "users", "allow_sample_data": False})
    res = execute_database_tool(call=call, state=session_state, original_question="add a user in users table")

    assert res.success is True
    assert session_state.pending_mutation is not None
    assert session_state.pending_mutation.table_name == "users"
    assert "username" in session_state.pending_mutation.missing_fields
    assert "email" in session_state.pending_mutation.missing_fields
    assert "full_name" in session_state.pending_mutation.missing_fields


def test_resume_pending_mutation_with_comma_separated_answers(session_state):
    """Verify providing comma-separated answers populates pending mutation and advances to approval."""
    # Setup pending mutation
    session_state.pending_mutation = PendingMutation(
        operation=MutationType.INSERT,
        table_name="users",
        values={},
        missing_fields=["username", "email", "full_name", "password"],
        allow_sample_data=False,
        question="add a user in users table",
    )

    # User answers: john_doe, john@example.com, John Doe, SecretPass123!
    mock_input = MagicMock(return_value="n")  # User enters 'n' at approval prompt
    resumed = resume_pending_mutation(
        user_response="john_doe, john@example.com, John Doe, SecretPass123!",
        state=session_state,
        user_input_func=mock_input,
    )
    assert resumed is True
    # The change preview was displayed and prompted for approval
    mock_input.assert_called_with("Apply this change? [y/N]: ")


def test_resume_pending_mutation_cancellation(session_state):
    """Verify user typing 'cancel' cancels pending mutation without modifying database."""
    session_state.pending_mutation = PendingMutation(
        operation=MutationType.INSERT,
        table_name="users",
        values={},
        missing_fields=["username", "email"],
    )
    resumed = resume_pending_mutation(
        user_response="cancel",
        state=session_state,
    )
    assert resumed is True
    assert session_state.pending_mutation is None


# -------------------------------------------------------------------------
# 6. Sample Data Creation With Approval
# -------------------------------------------------------------------------
def test_create_record_with_sample_data_prompts_approval_before_commit(session_state):
    """Verify random details request builds change plan and requires approval before writing."""
    mock_input = MagicMock(return_value="y")
    mock_executor = MagicMock()
    mock_executor.execute_mutation.return_value = MagicMock(is_success=True, status="COMMITTED", affected_rows=1, duration_ms=25.0)

    call = ToolCall(tool="create_record", arguments={"table_name": "users", "allow_sample_data": True})
    res = execute_database_tool(
        call=call,
        state=session_state,
        user_input_func=mock_input,
        executor=mock_executor,
        original_question="add a user in users table, the details can be random",
    )
    # Explicit approval prompted
    mock_input.assert_called_with("Apply this change? [y/N]: ")
    # Executed after approval
    mock_executor.execute_mutation.assert_called_once()
    assert res.success is True
    assert res.change_plan is not None
    assert res.change_plan.table_name == "users"
    assert "username" in res.change_plan.fields_to_set
    assert "password_hash" in res.change_plan.fields_to_set


# -------------------------------------------------------------------------
# 7. DDL Schema Definition Guidance
# -------------------------------------------------------------------------
def test_ddl_table_creation_requests_route_to_ddl_guidance(mock_users_schema, sample_profile, session_state):
    """Verify 'create table' and 'make me a table' are recognized as DDL requests, not queries or record insertions."""
    # Request 1: "create a table called users with id, name and email"
    call_ddl_1 = select_tool_for_user_request(
        "create a table called users with id, name and email",
        mock_users_schema,
        sample_profile,
        session_state,
    )
    assert call_ddl_1.tool in ("create_table", "ddl_guidance")
    assert call_ddl_1.arguments.get("table_name") == "users"
    assert "id" in call_ddl_1.arguments.get("columns_description", "")
    assert "name" in call_ddl_1.arguments.get("columns_description", "")
    assert "email" in call_ddl_1.arguments.get("columns_description", "")

    # Request 2: "make me a table for storing students"
    call_ddl_2 = select_tool_for_user_request(
        "make me a table for storing students",
        mock_users_schema,
        sample_profile,
        session_state,
    )
    assert call_ddl_2.tool in ("create_table", "ddl_guidance")
    assert call_ddl_2.arguments.get("table_name") == "students"

    # Execution of DDL guidance asks for approval and handles decline gracefully
    mock_input = MagicMock(return_value="n")
    res = execute_database_tool(
        call_ddl_1,
        session_state,
        user_input_func=mock_input,
        original_question="create a table called users with id, name and email",
    )
    mock_input.assert_called_with("Execute this table creation on 'athena'? [y/N]: ")
    assert res.success is True
    assert "CREATE TABLE" in res.output
    assert "users" in res.output


