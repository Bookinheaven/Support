"""Regression tests for general-purpose PostgreSQL workspace and SQL repair.

Covers the 10 requirements:
1. Request to create a table does not enter the SELECT-only generator.
2. CREATE TABLE repair preserves CREATE TABLE.
3. INSERT repair preserves INSERT.
4. UPDATE repair preserves UPDATE.
5. DELETE repair preserves DELETE.
6. SELECT repair continues to work.
7. Natural-language requests reach the appropriate registered tools.
8. Invalid or unsupported operations are handled safely.
9. DDL and mutations still require approval.
10. Existing read-only protections remain intact.
"""

from unittest.mock import MagicMock, patch
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.llm.models import GenerationResponse
from support.query.executor import QueryResult, ReadOnlyQueryExecutor
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.repair import (
    build_repair_prompt,
    detect_intended_operation,
    repair_sql_statement,
)
from support.query.validator import validate_sql_query
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.tools.executor import execute_database_tool
from support.tools.registry import DATABASE_TOOLS
from support.tools.selector import select_tool_for_user_request
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def test_schema():
    schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="testdb")
    users = TableSchema(
        name="users",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR"),
            ColumnSchema(name="email", data_type="VARCHAR"),
            ColumnSchema(name="status", data_type="VARCHAR"),
        ],
    )
    orders = TableSchema(
        name="orders",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER"),
            ColumnSchema(name="amount", data_type="NUMERIC"),
        ],
    )
    schema.tables["users"] = users
    schema.tables["orders"] = orders
    return schema


@pytest.fixture
def session_state(test_schema):
    state = WorkspaceSessionState(
        active_database="testdb",
        database_profile=ConnectionProfile(
            name="testdb", dialect=Dialect.POSTGRES, database="testdb"
        ),
    )
    state.cached_schema = test_schema
    return state


# ---------------------------------------------------------------------------
# 1. Request to create a table does not enter the SELECT-only generator
# ---------------------------------------------------------------------------

def test_create_table_request_selects_create_table_tool(session_state):
    """Verify that requests to create tables route to create_table, not query_database."""
    user_prompt = "create a table called products with id, title, and price"
    tool_call = select_tool_for_user_request(user_prompt, state=session_state, llm_service=None)
    assert tool_call.tool == "create_table"
    assert tool_call.arguments.get("table_name") == "products"


# ---------------------------------------------------------------------------
# 2. CREATE TABLE repair preserves CREATE TABLE
# ---------------------------------------------------------------------------

def test_create_table_repair_preserves_create_table():
    """Verify that a failed CREATE TABLE statement is repaired as a CREATE TABLE."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="CREATE TABLE products (\n    id SERIAL PRIMARY KEY,\n    name VARCHAR(255) NOT NULL\n);",
        provider="test",
        model="test-model",
    )

    failed_sql = "CREATE TABLE products (id SERIAL PRIMARY KEY, name VARCHAR);"
    error_msg = 'syntax error at or near ";"'
    
    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message=error_msg,
        original_question="create products table",
        dialect_name="postgres",
        schema_context="Table users (id INT, username VARCHAR);",
        llm_service=mock_llm,
    )

    assert repaired is not None
    assert "CREATE TABLE" in repaired.upper()
    assert "SELECT" not in repaired.upper()


def test_create_table_repair_rejects_conversion_to_select():
    """Verify that if LLM attempts to convert CREATE TABLE into a SELECT, it is rejected."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="SELECT id, name FROM products;",
        provider="test",
        model="test-model",
    )

    failed_sql = "CREATE TABLE products (id INT, name TEXT);"
    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message='relation "products" does not exist',
        original_question="create products table",
        dialect_name="postgres",
        schema_context="Table users (id INT, username VARCHAR);",
        llm_service=mock_llm,
    )

    # Conversion to SELECT must be rejected for a CREATE TABLE repair
    assert repaired is None
    assert "converted CREATE TABLE to SELECT" in explanation


# ---------------------------------------------------------------------------
# 3. INSERT repair preserves INSERT
# ---------------------------------------------------------------------------

def test_insert_repair_preserves_insert():
    """Verify that a failed INSERT statement is repaired as an INSERT."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="INSERT INTO users (id, username, email, status) VALUES (1, 'alice', 'alice@example.com', 'active');",
        provider="test",
        model="test-model",
    )

    failed_sql = "INSERT INTO users (id, username, email) VALUES (1, 'alice');"
    error_msg = 'null value in column "status" violates not-null constraint'

    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message=error_msg,
        original_question="insert a new user alice",
        dialect_name="postgres",
        schema_context="Table users (id INT, username VARCHAR, email VARCHAR, status VARCHAR);",
        llm_service=mock_llm,
    )

    assert repaired is not None
    assert repaired.strip().upper().startswith("INSERT INTO")


# ---------------------------------------------------------------------------
# 4. UPDATE repair preserves UPDATE
# ---------------------------------------------------------------------------

def test_update_repair_preserves_update():
    """Verify that a failed UPDATE statement is repaired as an UPDATE."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="UPDATE users SET status = 'inactive' WHERE id = 10;",
        provider="test",
        model="test-model",
    )

    failed_sql = "UPDATE users SET status = inactive WHERE id = 10;"
    error_msg = 'column "inactive" does not exist'

    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message=error_msg,
        original_question="deactivate user 10",
        dialect_name="postgres",
        schema_context="Table users (id INT, username VARCHAR, status VARCHAR);",
        llm_service=mock_llm,
    )

    assert repaired is not None
    assert repaired.strip().upper().startswith("UPDATE")
    assert "WHERE" in repaired.upper()


# ---------------------------------------------------------------------------
# 5. DELETE repair preserves DELETE
# ---------------------------------------------------------------------------

def test_delete_repair_preserves_delete():
    """Verify that a failed DELETE statement is repaired as a DELETE."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="DELETE FROM users WHERE id = 5;",
        provider="test",
        model="test-model",
    )

    failed_sql = "DELETE users WHERE id = 5;"
    error_msg = 'syntax error at or near "WHERE"'

    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message=error_msg,
        original_question="delete user 5",
        dialect_name="postgres",
        schema_context="Table users (id INT, username VARCHAR);",
        llm_service=mock_llm,
    )

    assert repaired is not None
    assert repaired.strip().upper().startswith("DELETE FROM")


# ---------------------------------------------------------------------------
# 6. SELECT repair continues to work
# ---------------------------------------------------------------------------

def test_select_repair_continues_to_work():
    """Verify that SELECT repairs continue to function properly."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="SELECT user_id, COUNT(*) FROM orders GROUP BY user_id;",
        provider="test",
        model="test-model",
    )

    failed_sql = "SELECT user_id, COUNT(*) FROM orders;"
    error_msg = 'column "orders.user_id" must appear in the GROUP BY clause'

    repaired, explanation = repair_sql_statement(
        failed_sql=failed_sql,
        error_message=error_msg,
        original_question="count orders per user",
        dialect_name="postgres",
        schema_context="Table orders (id INT, user_id INT, amount NUMERIC);",
        llm_service=mock_llm,
    )

    assert repaired is not None
    assert repaired.strip().upper().startswith("SELECT")
    assert "GROUP BY" in repaired.upper()


# ---------------------------------------------------------------------------
# 7. Natural-language requests reach the appropriate registered tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prompt,expected_tool",
    [
        ("show top 10 orders", "query_database"),
        ("insert into users (username, email) values ('bob', 'bob@test.com')", "create_record"),
        ("add a user in users table", "create_record"),
        ("update users set status = 'active' where id = 1", "update_records"),
        ("delete from users where id = 99", "delete_records"),
        ("create a table called logs with id and message", "create_table"),
        ("alter table users add column phone varchar", "alter_table"),
        ("create an index on users email", "create_index"),
    ],
)
def test_requests_reach_appropriate_tools(prompt, expected_tool, session_state):
    """Verify tool selector maps CRUD, DDL, and Query prompts to their registered tools."""
    tool_call = select_tool_for_user_request(prompt, state=session_state, llm_service=None)
    assert tool_call.tool == expected_tool


# ---------------------------------------------------------------------------
# 8. Invalid or unsupported operations are handled safely
# ---------------------------------------------------------------------------

def test_unsupported_or_invalid_sql_handled_safely(test_schema):
    """Verify validator safely rejects unparseable or dangerous commands."""
    # Syntax error
    val1 = validate_sql_query("SELECT FROM WHERE", dialect="postgres", schema=test_schema)
    assert not val1.is_valid
    assert "syntax error" in val1.status_message.lower()

    # Mutation rejected when allow_mutations=False
    val2 = validate_sql_query("DELETE FROM users;", dialect="postgres", schema=test_schema, allow_mutations=False)
    assert not val2.is_valid
    assert "only read-only select queries are permitted" in val2.status_message.lower()

    # Direct LIMIT on DELETE rejected in PostgreSQL
    val3 = validate_sql_query("DELETE FROM users LIMIT 5;", dialect="postgres", schema=test_schema, allow_mutations=True)
    assert not val3.is_valid
    assert "direct limit clause on delete is not supported in postgresql" in val3.status_message.lower()

    # Scope-aware: Unconditional DELETE allowed when allow_mutations=True as FULL_TABLE scope
    val4 = validate_sql_query("DELETE FROM users;", dialect="postgres", schema=test_schema, allow_mutations=True)
    assert val4.is_valid is True
    assert val4.mutation_scope == "FULL_TABLE"
    assert val4.is_unrestricted is True


# ---------------------------------------------------------------------------
# 9. DDL and mutations still require approval
# ---------------------------------------------------------------------------

def test_create_index_requires_approval(session_state):
    """Verify create_index prompts for approval and aborts when denied."""
    from support.tools.models import ToolCall

    tool_call = ToolCall(
        tool="create_index",
        arguments={
            "table_name": "users",
            "index_name": "idx_users_email",
            "columns": ["email"],
            "unique": False,
        },
    )

    res = execute_database_tool(
        tool_call,
        session_state,
        llm_service=None,
        user_input_func=lambda _: "n",
    )
    assert "cancelled by user" in (res.display_text or "").lower()


def test_insert_mutation_requires_approval(session_state):
    """Verify create_record prompts for approval and aborts when denied."""
    from support.tools.models import ToolCall

    tool_call = ToolCall(
        tool="create_record",
        arguments={
            "table_name": "users",
            "values": {"username": "charlie", "email": "charlie@example.com", "status": "active"},
        },
    )

    res = execute_database_tool(
        tool_call,
        session_state,
        llm_service=None,
        user_input_func=lambda _: "n",
    )
    assert res.success is False
    assert "declined" in (res.error or "").lower()


# ---------------------------------------------------------------------------
# 10. Existing read-only protections remain intact
# ---------------------------------------------------------------------------

def test_readonly_protections_reject_mutations_when_disallowed(test_schema):
    """Verify validate_sql_query with allow_mutations=False rejects INSERT, UPDATE, DELETE, DDL."""
    for sql in [
        "INSERT INTO users (username) VALUES ('test');",
        "UPDATE users SET username = 'test' WHERE id = 1;",
        "DELETE FROM users WHERE id = 1;",
        "CREATE TABLE test (id INT);",
        "DROP TABLE users;",
    ]:
        val = validate_sql_query(sql, dialect="postgres", schema=test_schema, allow_mutations=False)
        assert not val.is_valid
        assert "Only read-only SELECT queries are permitted" in val.status_message


# ---------------------------------------------------------------------------
# 11. 'creat a tabel for prodcuts' does not invent instruction words as columns
# ---------------------------------------------------------------------------

def test_create_table_typos_does_not_invent_instruction_columns(session_state):
    """Verify 'creat a tabel for prodcuts' generates valid table DDL without inventing instruction columns."""
    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nCREATE TABLE products (\n    id SERIAL PRIMARY KEY,\n    name VARCHAR(255) NOT NULL,\n    price NUMERIC(10, 2) NOT NULL,\n    description TEXT,\n    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n);\n```\nExplanation: Creates products table with standard domain columns.",
        provider="test",
        model="test-model",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("creat a tabel for prodcuts", session_state)

    ddl_upper = draft.sql.upper()
    assert "CREATE TABLE PRODUCTS" in ddl_upper
    # Ensure instruction words are never columns
    assert "CREAT VARCHAR" not in ddl_upper
    assert "TABEL VARCHAR" not in ddl_upper
    assert "PRODCUTS VARCHAR" not in ddl_upper
    assert draft.is_validated is True


def test_create_table_works_on_empty_database_schema(session_state):
    """Verify CREATE TABLE is not rejected when active database currently has 0 tables."""
    empty_schema = DatabaseSchema(dialect=Dialect.POSTGRES, database_name="empty_db")
    session_state.cached_schema = empty_schema

    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nCREATE TABLE items (id SERIAL PRIMARY KEY, title VARCHAR(255) NOT NULL);\n```\nExplanation: Creates items table.",
        provider="test",
        model="test-model",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("create table items with id and title", session_state)

    assert draft.is_validated is True
    assert "CREATE TABLE items" in draft.sql
    assert draft.validation_status == "Passed"


def test_ddl_generation_supports_multiple_dialects(session_state):
    """Verify LLM-driven DDL generator invokes the LLM with the target dialect."""
    from support.tools.executor import _generate_ddl_with_llm

    mock_llm = MagicMock()
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nCREATE TABLE products (id SERIAL PRIMARY KEY, name VARCHAR(255));\n```",
        provider="test",
        model="test-model",
    )

    pg_ddl = _generate_ddl_with_llm("create products table", Dialect.POSTGRES, mock_llm, session_state)
    assert "CREATE TABLE products" in pg_ddl
    assert mock_llm.generate.call_count == 1
    prompt_used = mock_llm.generate.call_args[1]["prompt"]
    assert "POSTGRES" in prompt_used.upper()

