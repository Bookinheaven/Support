"""Comprehensive tests for natural-language query generation, schema context, dialect support, AST validation, and safety."""

from unittest.mock import MagicMock, patch
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.llm.models import GenerationResponse, ProviderStatus
from support.llm.service import LLMService
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.schema_context import (
    format_schema_for_prompt,
    get_relevant_schema_context,
    get_relevant_tables,
)
from support.query.validator import validate_sql_query
from support.schema.models import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    TableSchema,
)
from support.workspace.commands import execute_slash_command
from support.workspace.session import run_workspace_session
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def sample_state():
    """Create a sample session state for workspace testing."""
    profile = ConnectionProfile(
        name="athena",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="athena",
        username="postgres",
    )
    return WorkspaceSessionState(
        active_database="athena",
        database_profile=profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )


@pytest.fixture
def ecommerce_schema():
    """Create a normalized mock ecommerce schema."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES)

    customers = TableSchema(
        name="customers",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="name", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(255)"),
            ColumnSchema(name="country", data_type="VARCHAR(50)"),
        ],
    )
    orders = TableSchema(
        name="orders",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="customer_id", data_type="INTEGER", nullable=False),
            ColumnSchema(name="total_amount", data_type="NUMERIC(10,2)", nullable=False),
            ColumnSchema(name="order_date", data_type="TIMESTAMP"),
        ],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["customer_id"],
                referred_table="customers",
                referred_columns=["id"],
            )
        ],
    )
    products = TableSchema(
        name="products",
        primary_key=["id"],
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="title", data_type="VARCHAR(100)"),
            ColumnSchema(name="price", data_type="NUMERIC(10,2)"),
        ],
    )
    schema.add_table(customers)
    schema.add_table(orders)
    schema.add_table(products)
    return schema


def test_sql_validator_accepts_readonly_select_and_ctes():
    """Verify validator permits standard SELECT queries and CTEs across dialects."""
    valid_cases = [
        ("SELECT id, name FROM customers WHERE country = 'IN' LIMIT 10", Dialect.POSTGRES),
        ("SELECT `id`, `name` FROM `customers` ORDER BY `id` DESC LIMIT 5", Dialect.MYSQL),
        ("SELECT id, name FROM customers LIMIT 10", Dialect.SQLITE),
        ("SELECT TOP (10) id, name FROM customers", Dialect.MSSQL),
        ("WITH ranked AS (SELECT id, name FROM customers) SELECT * FROM ranked", Dialect.POSTGRES),
        ("SELECT c.name, COUNT(o.id) FROM customers c JOIN orders o ON c.id = o.customer_id GROUP BY c.name", Dialect.POSTGRES),
        ("SELECT 1 UNION ALL SELECT 2", Dialect.POSTGRES),
    ]

    for sql, dialect in valid_cases:
        is_valid, msg = validate_sql_query(sql, dialect=dialect)
        assert is_valid is True, f"Expected valid for: {sql}, got {msg}"
        assert msg == "Passed"


def test_sql_validator_rejects_dml_ddl_and_administrative_operations():
    """Verify validator strictly rejects INSERT, UPDATE, DELETE, MERGE, DROP, CREATE, ALTER, TRUNCATE."""
    prohibited_cases = [
        "INSERT INTO customers (id, name) VALUES (1, 'Alice')",
        "UPDATE customers SET name = 'Bob' WHERE id = 1",
        "DELETE FROM customers WHERE id = 1",
        "DROP TABLE customers",
        "CREATE TABLE test (id int)",
        "ALTER TABLE customers ADD COLUMN age int",
        "TRUNCATE TABLE orders",
        "MERGE INTO customers USING other ON 1=1 WHEN MATCHED THEN UPDATE SET name='x'",
    ]

    for sql in prohibited_cases:
        is_valid, msg = validate_sql_query(sql, dialect=Dialect.POSTGRES)
        assert is_valid is False, f"Expected rejection for: {sql}"
        assert "Rejected" in msg


def test_sql_validator_rejects_multiple_statements_and_nested_mutations():
    """Verify validator rejects multi-statement SQL injections and nested mutations inside CTEs."""
    malicious_cases = [
        "SELECT 1; DROP TABLE customers;",
        "SELECT id FROM customers; DELETE FROM orders;",
        "WITH bad AS (DELETE FROM customers RETURNING *) SELECT * FROM bad",
        "SELECT * FROM customers WHERE id = (DELETE FROM orders RETURNING id)",
        "INVALID SQL FROM WHERE",
        "",
    ]

    for sql in malicious_cases:
        is_valid, msg = validate_sql_query(sql, dialect=Dialect.POSTGRES)
        assert is_valid is False, f"Expected rejection for: {sql}"
        assert "Rejected" in msg


def test_schema_context_extraction_and_relevance_ranking(ecommerce_schema):
    """Verify deterministic schema relevance ranking filters tables matching question keywords."""
    context, is_full, summary, tables = get_relevant_schema_context(
        ecommerce_schema, question="Show customer orders and spending"
    )
    assert "TABLE customers (" in context
    assert "TABLE orders (" in context
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id)" in context
    assert "customers" in tables
    assert "orders" in tables


def test_query_generator_postgresql_draft(ecommerce_schema):
    """Verify QueryGenerator generates validated PostgreSQL SQL drafts using mocked model response."""
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(
        active_database="pg_test",
        database_profile=profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = ecommerce_schema

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text=(
            "```sql\n"
            "SELECT c.id, c.name, SUM(o.total_amount) AS total_spent\n"
            "FROM customers c\n"
            "JOIN orders o ON c.id = o.customer_id\n"
            "GROUP BY c.id, c.name\n"
            "ORDER BY total_spent DESC\n"
            "LIMIT 10;\n"
            "```\n"
            "Explanation: Ranks customers by total spending."
        ),
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Which customers have spent the most?", state=state)

    assert draft.is_validated is True
    assert draft.validation_status == "Passed"
    assert "SELECT c.id" in draft.sql
    assert "LIMIT 10" in draft.sql
    assert "Ranks customers" in draft.explanation
    assert draft.execution_status == "Not performed"
    assert state.model_generation_tested is True
    assert state.model_generation_status == "Passed"


def test_query_generator_mysql_draft(ecommerce_schema):
    """Verify QueryGenerator instructs MySQL dialect and produces validated draft."""
    profile = ConnectionProfile(name="mysql_test", dialect=Dialect.MYSQL, database="shop")
    state = WorkspaceSessionState(
        active_database="mysql_test",
        database_profile=profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = ecommerce_schema

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT `id`, `name` FROM `customers` LIMIT 5;\n```\nExplanation: Lists first 5 customers.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show first 5 customers", state=state)

    # Check that prompt specified MySQL
    called_prompt = mock_llm.generate.call_args[1]["prompt"]
    assert "MYSQL" in called_prompt

    assert draft.is_validated is True
    assert draft.validation_status == "Passed"
    assert "LIMIT 5" in draft.sql


def test_query_generator_sql_server_draft(ecommerce_schema):
    """Verify QueryGenerator instructs SQL Server (T-SQL) dialect and accepts TOP syntax."""
    profile = ConnectionProfile(name="mssql_test", dialect=Dialect.MSSQL, database="erp")
    state = WorkspaceSessionState(
        active_database="mssql_test",
        database_profile=profile,
        active_provider="ollama",
        active_model="qwen2.5-coder:7b",
    )
    state.cached_schema = ecommerce_schema

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT TOP (10) id, name FROM customers;\n```\nExplanation: Selects top 10 customers.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Show 10 customers", state=state)

    called_prompt = mock_llm.generate.call_args[1]["prompt"]
    assert "MSSQL" in called_prompt

    assert draft.is_validated is True
    assert draft.validation_status == "Passed"
    assert "TOP (10)" in draft.sql


def test_query_generator_follow_up_uses_bounded_history(ecommerce_schema):
    """Verify follow-up questions include previous question and previous SQL draft in prompt context."""
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(
        active_database="pg_test",
        database_profile=profile,
    )
    state.cached_schema = ecommerce_schema

    # Prior turn in history
    state.add_message("user", "Which customers spent the most?")
    state.add_message(
        "assistant",
        "Ranks customers by spending.",
        metadata={"sql": "SELECT id, name FROM customers LIMIT 10"},
    )

    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text=(
            "```sql\n"
            "SELECT id, name FROM customers WHERE country = 'Germany' LIMIT 10;\n"
            "```\n"
            "Explanation: Filters previous query to Germany."
        ),
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Only include customers from Germany", state=state)

    called_prompt = mock_llm.generate.call_args[1]["prompt"]
    assert "Which customers spent the most?" in called_prompt
    assert "SELECT id, name FROM customers LIMIT 10" in called_prompt
    assert "Only include customers from Germany" in called_prompt

    assert draft.is_validated is True
    assert "country = 'Germany'" in draft.sql


def test_stale_generation_rejected_when_database_switches(ecommerce_schema):
    """Verify in-flight generation is marked stale if active database switches before completion."""
    profile1 = ConnectionProfile(name="db_initial", dialect=Dialect.POSTGRES, database="db1")
    state = WorkspaceSessionState(
        active_database="db_initial",
        database_profile=profile1,
    )
    state.cached_schema = ecommerce_schema

    mock_llm = MagicMock(spec=LLMService)

    def slow_generate(*args, **kwargs):
        # Simulate active database switch during generation
        profile2 = ConnectionProfile(name="db_new", dialect=Dialect.MYSQL, database="db2")
        state.set_database("db_new", profile=profile2)
        return GenerationResponse(
            text="```sql\nSELECT 1;\n```\nExplanation: test",
            provider="ollama",
            model="qwen2.5-coder:7b",
        )

    mock_llm.generate.side_effect = slow_generate

    generator = QueryGenerator(llm_service=mock_llm)
    draft = generator.generate_draft("Any query", state=state)

    assert draft.is_validated is False
    assert "Stale generation result" in draft.validation_status


def test_mongodb_nl_query_boundary(sample_state):
    """Verify MongoDB databases reject SQL generation with a clear informative status."""
    mongo_profile = ConnectionProfile(
        name="mongo_app",
        dialect=Dialect.MONGODB,
        host="localhost",
        port=27017,
        database="analytics",
    )
    state = WorkspaceSessionState(
        active_database="mongo_app",
        database_profile=mongo_profile,
    )

    generator = QueryGenerator()
    draft = generator.generate_draft("Find active users", state=state)

    assert draft.is_validated is False
    assert "MongoDB" in draft.explanation
    assert draft.dialect == "mongodb"


def test_generated_sql_is_never_executed(ecommerce_schema):
    """Verify that QueryGenerator and session loop never invoke database execution methods."""
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(
        active_database="pg_test",
        database_profile=profile,
    )
    state.cached_schema = ecommerce_schema

    mock_db = MagicMock(spec=DatabaseService)
    mock_llm = MagicMock(spec=LLMService)
    mock_llm.generate.return_value = GenerationResponse(
        text="```sql\nSELECT id FROM customers;\n```\nExplanation: Customer IDs.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )

    generator = QueryGenerator(db_service=mock_db, llm_service=mock_llm)
    draft = generator.generate_draft("Get IDs", state=state)

    assert draft.is_validated is True
    assert draft.execution_status == "Not performed"

    # Verify no execution methods were touched on database service
    assert not hasattr(mock_db, "execute")
    mock_db.test_profile.assert_not_called()


def test_workspace_commands_modular_dispatch(sample_state):
    """Verify refactored command dispatcher handles commands properly."""
    assert execute_slash_command("/help", state=sample_state) is False
    assert execute_slash_command("/clear", state=sample_state) is False
    assert execute_slash_command("/status", state=sample_state) is False
    assert execute_slash_command("/schema", state=sample_state) is False
    assert execute_slash_command("/unknown_cmd", state=sample_state) is False
    assert execute_slash_command("/exit", state=sample_state) is True
    assert execute_slash_command("/quit", state=sample_state) is True


def test_sql_validator_rejects_select_into_and_file_exports():
    """Verify validator strictly rejects SELECT INTO (table creation) and MySQL file export clauses."""
    # SELECT INTO creates new tables
    for dial in (Dialect.POSTGRES, Dialect.SQLITE, Dialect.MSSQL):
        is_valid, msg = validate_sql_query("SELECT * INTO new_table FROM customers", dialect=dial)
        assert is_valid is False
        assert "SELECT INTO" in msg

    # File export clauses in MySQL
    for file_sql in (
        "SELECT * INTO OUTFILE '/tmp/users.txt' FROM customers",
        "SELECT * INTO DUMPFILE '/tmp/dump.bin' FROM customers",
        "SELECT * FROM customers INTO OUTFILE '/tmp/users.txt'",
    ):
        is_valid, msg = validate_sql_query(file_sql, dialect=Dialect.MYSQL)
        assert is_valid is False
        assert "export" in msg.lower() or "syntax" in msg.lower()


def test_sql_validator_rejects_locking_clauses_and_hints():
    """Verify validator strictly rejects FOR UPDATE, FOR SHARE, and T-SQL locking hints."""
    # Postgres / MySQL locking clauses
    lock_cases = [
        ("SELECT * FROM customers FOR UPDATE", Dialect.POSTGRES),
        ("SELECT * FROM customers FOR SHARE", Dialect.POSTGRES),
        ("SELECT * FROM customers FOR NO KEY UPDATE", Dialect.POSTGRES),
        ("SELECT * FROM customers LOCK IN SHARE MODE", Dialect.MYSQL),
    ]
    for sql, dial in lock_cases:
        is_valid, msg = validate_sql_query(sql, dialect=dial)
        assert is_valid is False
        assert "locking" in msg.lower()

    # T-SQL table locking hints
    tsql_hints = [
        "SELECT * FROM customers WITH (UPDLOCK)",
        "SELECT * FROM customers WITH (XLOCK)",
        "SELECT * FROM customers WITH (HOLDLOCK)",
        "SELECT * FROM customers WITH (TABLOCKX)",
    ]
    for sql in tsql_hints:
        is_valid, msg = validate_sql_query(sql, dialect=Dialect.MSSQL)
        assert is_valid is False
        assert "locking hint" in msg.lower()


def test_sql_validator_schema_element_verification(ecommerce_schema):
    """Verify validator detects unknown tables and columns against the discovered schema."""
    # 1. Valid query with known tables and columns
    valid_res = validate_sql_query(
        "SELECT id, name, country FROM customers WHERE email IS NOT NULL",
        dialect=Dialect.POSTGRES,
        schema=ecommerce_schema,
    )
    assert valid_res.is_valid is True
    assert valid_res.is_schema_verified is True
    assert valid_res.unknown_tables == []
    assert "Verified" in valid_res.schema_status

    # 2. Query referencing hallucinated tables (e.g. flights and bookings)
    hallucinated_res = validate_sql_query(
        "SELECT f.flight_no FROM flights f JOIN bookings b ON f.id = b.flight_id",
        dialect=Dialect.POSTGRES,
        schema=ecommerce_schema,
    )
    assert hallucinated_res.is_valid is True
    assert hallucinated_res.is_schema_verified is False
    assert "flights" in hallucinated_res.unknown_tables
    assert "bookings" in hallucinated_res.unknown_tables
    assert "Warning" in hallucinated_res.schema_status

    # 3. Query with CTE: CTE alias is not falsely flagged as unknown table
    cte_res = validate_sql_query(
        "WITH active_cust AS (SELECT id, name FROM customers) SELECT * FROM active_cust",
        dialect=Dialect.POSTGRES,
        schema=ecommerce_schema,
    )
    assert cte_res.is_valid is True
    assert cte_res.is_schema_verified is True
    assert cte_res.unknown_tables == []


def test_schema_context_preserves_bridge_tables_for_joins():
    """Verify relevance ranking includes bridge tables connecting two scored tables."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES)
    # Create 14 dummy tables to exceed max_tables limit (12)
    for i in range(1, 15):
        schema.add_table(TableSchema(name=f"t_{i}", columns=[ColumnSchema(name="id")]))

    # Target table A (orders) and Target table B (products)
    orders = TableSchema(name="orders", columns=[ColumnSchema(name="id"), ColumnSchema(name="total")])
    products = TableSchema(name="products", columns=[ColumnSchema(name="id"), ColumnSchema(name="price")])
    # Bridge table: order_items (bridges orders and products via FKs)
    order_items = TableSchema(
        name="order_items",
        columns=[ColumnSchema(name="id"), ColumnSchema(name="order_id"), ColumnSchema(name="product_id")],
        foreign_keys=[
            ForeignKeySchema(constrained_columns=["order_id"], referred_table="orders", referred_columns=["id"]),
            ForeignKeySchema(constrained_columns=["product_id"], referred_table="products", referred_columns=["id"]),
        ],
    )
    schema.add_table(orders)
    schema.add_table(products)
    schema.add_table(order_items)

    # Query mentions orders and products, not order_items
    selected = get_relevant_tables(schema, question="Show total from orders and product price", max_tables=8)
    selected_names = [t.name for t in selected]

    assert "orders" in selected_names
    assert "products" in selected_names
    # Bridge table order_items MUST be preserved
    assert "order_items" in selected_names


def test_schema_command_and_deliberate_refresh(sample_state, ecommerce_schema):
    """Verify /schema command displays schema and /schema refresh invalidates cache."""
    mock_db = MagicMock(spec=DatabaseService)

    sample_state.cached_schema = ecommerce_schema

    # 1. Plain /schema uses cached schema
    assert execute_slash_command("/schema", state=sample_state, db_service=mock_db) is False

    # 2. /schema refresh clears and reloads
    with patch("support.query.schema_context.inspect_database_schema", return_value=ecommerce_schema) as mock_inspect:
        assert execute_slash_command("/schema refresh", state=sample_state, db_service=mock_db) is False
        mock_inspect.assert_called_once()
        assert sample_state.cached_schema == ecommerce_schema


def test_display_query_draft_renders_all_distinct_claims():
    """Verify display_query_draft clearly distinguishes parsing, validation, schema check, and unexecuted status."""
    from support.query.display import display_query_draft
    from io import StringIO
    from rich.console import Console

    test_console = Console(file=StringIO(), color_system=None)
    with patch("support.query.display.console", test_console):
        draft = QueryDraft(
            question="List users",
            sql="SELECT id, username FROM users LIMIT 5;",
            explanation="Lists 5 users.",
            dialect="postgres",
            validation_status="Passed",
            is_validated=True,
            is_parsed=True,
            schema_status="Verified: References 1 known table(s) (users)",
            is_schema_verified=True,
            execution_status="Not performed",
            schema_coverage="Relevant subset (8 of 18 tables)",
        )
        display_query_draft(draft)

        output = test_console.file.getvalue()
        assert "Draft SQL" in output
        assert "SELECT id, username FROM users LIMIT 5;" in output
        assert "Explanation:" in output
        assert "Schema Context: Relevant subset (8 of 18 tables)" in output
        assert "Parsing:    Successful" in output
        assert "Validation: Passed" in output
        assert "Schema:     Verified: References 1 known table(s) (users)" in output
        assert "Execution:  Not performed" in output
