"""Focused tests for schema-context construction, conservative selection, and conversational context preservation."""

import pytest
from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
)
from support.workspace.state import WorkspaceSessionState
from support.query.schema_context import (
    get_relevant_tables,
    get_relevant_schema_context,
)
from support.query.generator import QueryGenerator


def _build_test_ecommerce_schema() -> DatabaseSchema:
    """Build a representative 5-table schema: organizations, users, orders, order_items, students."""
    orgs = TableSchema(
        name="organizations",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="name", data_type="VARCHAR", nullable=False),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        primary_key=["id"],
    )

    users = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="org_id", data_type="INTEGER"),
            ColumnSchema(name="full_name", data_type="VARCHAR"),
            ColumnSchema(name="email", data_type="VARCHAR", nullable=False),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["org_id"],
                referred_table="organizations",
                referred_columns=["id"],
            )
        ],
    )

    orders = TableSchema(
        name="orders",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER"),
            ColumnSchema(name="order_code", data_type="VARCHAR"),
            ColumnSchema(name="total_amount", data_type="NUMERIC"),
            ColumnSchema(name="status", data_type="VARCHAR"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["user_id"],
                referred_table="users",
                referred_columns=["id"],
            )
        ],
    )

    order_items = TableSchema(
        name="order_items",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="order_id", data_type="INTEGER"),
            ColumnSchema(name="product_name", data_type="VARCHAR"),
            ColumnSchema(name="quantity", data_type="INTEGER"),
            ColumnSchema(name="unit_price", data_type="NUMERIC"),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeySchema(
                constrained_columns=["order_id"],
                referred_table="orders",
                referred_columns=["id"],
            )
        ],
    )

    students = TableSchema(
        name="students",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="name", data_type="VARCHAR"),
            ColumnSchema(name="email", data_type="VARCHAR"),
        ],
        primary_key=["id"],
    )

    schema = DatabaseSchema(dialect=Dialect.POSTGRES)
    for t in (orgs, users, orders, order_items, students):
        schema.tables[t.name] = t
    return schema


def _build_large_schema(table_count: int = 15) -> DatabaseSchema:
    """Build a schema with > 12 tables."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES)
    for i in range(1, table_count + 1):
        tbl = TableSchema(
            name=f"table_{i:02d}",
            columns=[
                ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
                ColumnSchema(name=f"col_{i}", data_type="VARCHAR"),
            ],
            primary_key=["id"],
        )
        schema.tables[tbl.name] = tbl
    return schema


# 1. A single-table query sends only the relevant schema
def test_single_table_query_sends_only_relevant_schema():
    schema = _build_test_ecommerce_schema()
    selected = get_relevant_tables(schema, "Show the first five rows from the users table.")
    selected_names = [t.name for t in selected]
    assert selected_names == ["users"]
    assert "students" not in selected_names
    assert "orders" not in selected_names
    assert "organizations" not in selected_names


# 2. A two-table join includes the necessary FK relationship
def test_two_table_join_includes_necessary_fk_relationship():
    schema = _build_test_ecommerce_schema()
    selected = get_relevant_tables(
        schema, "Find all orders along with the user email for each order."
    )
    selected_names = set(t.name for t in selected)
    assert "orders" in selected_names
    assert "users" in selected_names
    assert "students" not in selected_names

    # Check that orders has the foreign key to users
    orders_table = next(t for t in selected if t.name == "orders")
    assert any(fk.referred_table == "users" for fk in orders_table.foreign_keys)


# 3. An aggregation across multiple related tables includes required bridge tables
def test_aggregation_across_multiple_related_tables_includes_bridge_tables():
    schema = _build_test_ecommerce_schema()
    # Query mentions organization and revenue / item count (which are in order_items)
    selected = get_relevant_tables(
        schema, "Calculate the total revenue and item count per organization."
    )
    selected_names = set(t.name for t in selected)
    assert "organizations" in selected_names
    assert "order_items" in selected_names
    # Bridge tables connecting organizations to order_items: users and orders
    assert "orders" in selected_names
    assert "users" in selected_names
    assert "students" not in selected_names


# 4. A follow-up retains relevant tables from previous SQL drafts
def test_follow_up_retains_relevant_tables_from_previous_sql_draft():
    schema = _build_test_ecommerce_schema()
    # Turn 1 generated SQL referencing orders and users
    prev_sql = "SELECT o.id, u.email FROM orders o JOIN users u ON o.user_id = u.id"
    # Turn 2 asks a follow-up without mentioning table names
    selected = get_relevant_tables(
        schema,
        question="Filter those to this month.",
        conversational_tables=["orders", "users"],
    )
    selected_names = set(t.name for t in selected)
    assert "orders" in selected_names
    assert "users" in selected_names
    assert "students" not in selected_names


# 5. A follow-up referencing a query from two turns earlier retains its required tables
def test_follow_up_referencing_query_two_turns_earlier_retains_tables():
    schema = _build_test_ecommerce_schema()
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=ConnectionProfile(name="dev-pg", dialect=Dialect.POSTGRES, database="support_dev"),
    )
    state.cached_schema = schema

    # Turn 1:
    state.add_message("user", "Show orders with user emails.")
    state.add_message(
        "assistant",
        "Generated draft",
        metadata={"draft_sql": "SELECT o.id, u.email FROM orders o JOIN users u ON o.user_id = u.id;"},
    )
    # Turn 2: informational check
    state.add_message("user", "What is the row count?")
    state.add_message(
        "assistant",
        "Generated count",
        metadata={"draft_sql": "SELECT COUNT(*) FROM orders;"},
    )

    # Turn 3: User says "Now group them by organization."
    generator = QueryGenerator()
    captured_prompts = []
    class MockLLM:
        def generate(self, prompt, **kwargs):
            from support.llm.models import GenerationResponse
            captured_prompts.append(prompt)
            return GenerationResponse(text="```sql\nSELECT 1;\n```", provider="mock", model="mock")

    generator.llm_service = MockLLM()
    draft = generator.generate_draft("Now group them by organization.", state)

    # Check that orders, users, and organizations are all in the generated prompt
    assert len(captured_prompts) == 1
    prompt_text = captured_prompts[0]
    assert "TABLE orders (" in prompt_text
    assert "TABLE users (" in prompt_text
    assert "TABLE organizations (" in prompt_text
    assert "TABLE students (" not in prompt_text


# 6. An ambiguous query falls back safely
def test_ambiguous_query_falls_back_safely():
    schema = _build_test_ecommerce_schema()
    # Completely generic query that matches no specific table or column
    selected = get_relevant_tables(schema, "Explain the database structure.")
    # Should fall back to providing available tables rather than an empty list
    assert len(selected) > 0
    assert len(selected) == len(schema.tables)


# 7. A database with three or fewer tables still sends the full schema
def test_database_with_three_or_fewer_tables_sends_full_schema():
    small_schema = DatabaseSchema(dialect=Dialect.POSTGRES)
    t1 = TableSchema(name="authors", columns=[ColumnSchema(name="id", is_primary_key=True)])
    t2 = TableSchema(name="books", columns=[ColumnSchema(name="id", is_primary_key=True)])
    t3 = TableSchema(name="publishers", columns=[ColumnSchema(name="id", is_primary_key=True)])
    small_schema.tables["authors"] = t1
    small_schema.tables["books"] = t2
    small_schema.tables["publishers"] = t3

    selected = get_relevant_tables(small_schema, "Show me authors.")
    assert len(selected) == 3
    selected_names = set(t.name for t in selected)
    assert selected_names == {"authors", "books", "publishers"}


# 8. A schema with more than twelve tables does not automatically send everything
def test_schema_with_more_than_twelve_tables_does_not_send_everything():
    large_schema = _build_large_schema(table_count=16)
    selected = get_relevant_tables(large_schema, "Show rows from table_05.")
    selected_names = [t.name for t in selected]
    assert selected_names == ["table_05"]
    assert len(selected) < len(large_schema.tables)


# 9. SQL drafts with aliases and schema-qualified table names are handled correctly
def test_sql_drafts_with_aliases_and_schema_qualification():
    import sqlglot
    from sqlglot import exp

    draft_sql = (
        "SELECT o.id, u.email, org.name "
        "FROM public.orders o "
        "JOIN public.users u ON o.user_id = u.id "
        "JOIN public.organizations org ON u.org_id = org.id;"
    )

    parsed = sqlglot.parse_one(draft_sql, read="postgres")
    tables = [tbl.name.lower() for tbl in parsed.find_all(exp.Table) if tbl.name]
    assert "orders" in tables
    assert "users" in tables
    assert "organizations" in tables
    assert "o" not in tables
    assert "u" not in tables
    assert "org" not in tables


# 10. Existing schema cache behavior remains unchanged
def test_existing_schema_cache_behavior_remains_unchanged():
    state = WorkspaceSessionState(
        active_database="dev-pg",
        database_profile=ConnectionProfile(name="dev-pg", dialect=Dialect.POSTGRES, database="support_dev"),
    )
    assert state.cached_schema is None

    mock_schema = _build_test_ecommerce_schema()
    state.cached_schema = mock_schema

    # Setting a new database invalidates cached_schema
    state.set_database("dev-sqlite", ConnectionProfile(name="dev-sqlite", dialect=Dialect.SQLITE, database="test.db"))
    assert state.cached_schema is None
