"""Tests for scope-aware mutation handling, dialect-aware validation, and approval gates."""

from unittest.mock import MagicMock
import pytest

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.query.generator import QueryGenerator
from support.query.models import QueryDraft
from support.query.validator import analyze_mutation_scope, validate_sql_query
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.workspace.execution_commands import handle_conversational_execution, handle_run_command
from support.workspace.state import WorkspaceSessionState
import sqlglot


@pytest.fixture
def mutation_schema():
    """Database schema with users, orders, and temporary_logs tables."""
    users_tbl = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False),
            ColumnSchema(name="email", data_type="VARCHAR(100)", nullable=False),
            ColumnSchema(name="status", data_type="VARCHAR(20)", nullable=False),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False),
        ],
        primary_key=["id"],
    )
    orders_tbl = TableSchema(
        name="orders",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER", nullable=False),
            ColumnSchema(name="total_amount", data_type="NUMERIC(10,2)", nullable=False),
            ColumnSchema(name="status", data_type="VARCHAR(20)", nullable=False),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False),
        ],
        primary_key=["id"],
    )
    logs_tbl = TableSchema(
        name="temporary_logs",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="message", data_type="TEXT", nullable=False),
            ColumnSchema(name="created_at", data_type="TIMESTAMP", nullable=False),
        ],
        primary_key=["id"],
    )
    return DatabaseSchema(
        dialect=Dialect.POSTGRES,
        tables={"users": users_tbl, "orders": orders_tbl, "temporary_logs": logs_tbl},
    )


# 1. DELETE with WHERE
def test_delete_with_where(mutation_schema):
    sql = "DELETE FROM users WHERE status = 'inactive';"
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.operation == "DELETE"
    assert res.mutation_scope == "FILTERED"
    assert res.is_unrestricted is False
    assert res.mutation_limit is None


# 2. DELETE with explicit row limit
def test_delete_with_explicit_row_limit(mutation_schema):
    # MySQL native direct LIMIT
    mysql_sql = "DELETE FROM users ORDER BY created_at ASC, id ASC LIMIT 5;"
    res_mysql = validate_sql_query(mysql_sql, dialect="mysql", schema=mutation_schema, allow_mutations=True)
    assert res_mysql.is_valid is True
    assert res_mysql.mutation_scope == "BOUNDED"
    assert res_mysql.mutation_limit == 5
    assert res_mysql.is_unrestricted is False


# 3. DELETE with deterministic ordering
def test_delete_with_deterministic_ordering(mutation_schema):
    # PostgreSQL subquery with deterministic ORDER BY
    pg_sql = "DELETE FROM users WHERE id IN (SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 5);"
    res = validate_sql_query(pg_sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.mutation_scope == "BOUNDED"
    assert res.mutation_limit == 5
    assert res.is_ordered is True


# 4. DELETE with CTE-based limiting
def test_delete_with_cte_limiting(mutation_schema):
    cte_sql = (
        "WITH to_delete AS ("
        "  SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 10"
        ") DELETE FROM users WHERE id IN (SELECT id FROM to_delete);"
    )
    res = validate_sql_query(cte_sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.mutation_scope == "BOUNDED"
    assert res.mutation_limit == 10
    assert res.is_ordered is True


# 5. Explicit full-table DELETE
def test_explicit_full_table_delete(mutation_schema):
    sql = "DELETE FROM temporary_logs;"
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.operation == "DELETE"
    assert res.mutation_scope == "FULL_TABLE"
    assert res.is_unrestricted is True
    assert res.mutation_limit is None
    assert "Full-table" in res.status_message


# 6. UPDATE with WHERE
def test_update_with_where(mutation_schema):
    sql = "UPDATE orders SET status = 'archived' WHERE created_at < '2025-01-01';"
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.operation == "UPDATE"
    assert res.mutation_scope == "FILTERED"
    assert res.is_unrestricted is False


# 7. Bounded UPDATE
def test_bounded_update(mutation_schema):
    # PostgreSQL bounded update with filter and limit
    pg_sql = (
        "UPDATE orders SET status = 'archived' WHERE id IN ("
        "  SELECT id FROM orders WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 5"
        ");"
    )
    res = validate_sql_query(pg_sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.operation == "UPDATE"
    assert res.mutation_scope == "BOUNDED_FILTERED"
    assert res.mutation_limit == 5
    assert res.is_ordered is True


# 8. Explicit full-table UPDATE
def test_explicit_full_table_update(mutation_schema):
    sql = "UPDATE orders SET status = 'archived';"
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.operation == "UPDATE"
    assert res.mutation_scope == "FULL_TABLE"
    assert res.is_unrestricted is True
    assert "Full-table" in res.status_message


def test_ambiguous_delete_quantity_triggers_clarification(mutation_schema):
    mock_llm = MagicMock()
    clarification_msg = (
        "Clarification:\n"
        "Which 5 users should be selected for deletion (for example: the oldest users, newest users, or by ID)?"
    )
    mock_llm.generate.return_value.text = clarification_msg
    mock_llm.generate.return_value.content = clarification_msg

    mock_db = MagicMock()
    mock_profile = ConnectionProfile(name="test_pg", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(active_database="test_pg", database_profile=mock_profile)
    state.cached_schema = mutation_schema

    generator = QueryGenerator(db_service=mock_db, llm_service=mock_llm)
    draft = generator.generate_draft("Delete 5 users from users table.", state)

    assert draft.is_clarification is True
    assert "Which 5 users" in draft.clarification
    assert not draft.sql


# 10. Unsupported or invalid limit syntax is rejected
def test_unsupported_direct_limit_rejected_in_postgres_and_sqlite(mutation_schema):
    # Direct LIMIT on DELETE in PostgreSQL
    pg_invalid = "DELETE FROM users LIMIT 5;"
    res_pg = validate_sql_query(pg_invalid, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res_pg.is_valid is False
    assert "Direct LIMIT clause on DELETE is not supported in PostgreSQL" in res_pg.status_message

    # Direct LIMIT on DELETE in SQLite
    sqlite_invalid = "DELETE FROM users LIMIT 5;"
    res_sqlite = validate_sql_query(sqlite_invalid, dialect="sqlite", schema=mutation_schema, allow_mutations=True)
    assert res_sqlite.is_valid is False
    assert "Direct LIMIT clause on DELETE is not supported in SQLite" in res_sqlite.status_message

    # Direct LIMIT on UPDATE in PostgreSQL
    pg_upd_invalid = "UPDATE orders SET status = 'archived' LIMIT 5;"
    res_upd_pg = validate_sql_query(pg_upd_invalid, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res_upd_pg.is_valid is False
    assert "Direct LIMIT clause on UPDATE is not supported in PostgreSQL" in res_upd_pg.status_message


# 11. PostgreSQL SQL generation (subquery-bounded with deterministic tie-breaker)
def test_postgresql_sql_generation_validates(mutation_schema):
    pg_sql = (
        "DELETE FROM users "
        "WHERE id IN ("
        "  SELECT id FROM users "
        "  ORDER BY created_at ASC, id ASC "
        "  LIMIT 5"
        ");"
    )
    res = validate_sql_query(pg_sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_ordered is True
    assert res.mutation_limit == 5


# 12. MySQL SQL generation (native direct limit)
def test_mysql_sql_generation_validates(mutation_schema):
    mysql_sql = "DELETE FROM users ORDER BY created_at ASC, id ASC LIMIT 5;"
    res = validate_sql_query(mysql_sql, dialect="mysql", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_ordered is True
    assert res.mutation_limit == 5


# 13. SQLite SQL generation (subquery-bounded)
def test_sqlite_sql_generation_validates(mutation_schema):
    sqlite_sql = (
        "DELETE FROM users "
        "WHERE id IN ("
        "  SELECT id FROM users "
        "  ORDER BY created_at ASC, id ASC "
        "  LIMIT 5"
        ");"
    )
    res = validate_sql_query(sqlite_sql, dialect="sqlite", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_ordered is True
    assert res.mutation_limit == 5


# 14. SQL Server SQL generation (CTE with TOP)
def test_sqlserver_sql_generation_validates(mutation_schema):
    tsql_sql = (
        "WITH cte AS ("
        "  SELECT TOP (5) * FROM users "
        "  ORDER BY created_at ASC, id ASC"
        ") DELETE FROM cte;"
    )
    res = validate_sql_query(tsql_sql, dialect="mssql", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_ordered is True
    assert res.mutation_limit == 5


# 15. All mutations still require approval (never auto-execute)
def test_mutations_always_require_human_approval(mutation_schema):
    profile = ConnectionProfile(name="pg_test", dialect=Dialect.POSTGRES, database="testdb")
    state = WorkspaceSessionState(active_database="pg_test", database_profile=profile)
    state.cached_schema = mutation_schema

    draft = QueryDraft(
        question="Delete the oldest 5 users",
        sql="DELETE FROM users WHERE id IN (SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 5);",
        explanation="Deletes oldest 5 users",
        dialect="postgres",
        validation_status="Passed: Bounded DELETE (limit: 5 rows)",
        is_validated=True,
        is_readonly=False,
        operation="DELETE",
        target_table="users",
        mutation_scope="BOUNDED",
        mutation_limit=5,
    )
    state.last_query_draft = draft

    # Mock user input saying 'no'
    mock_input_no = MagicMock(return_value="n")
    mock_db = MagicMock()
    executed = handle_conversational_execution(draft=draft, state=state, user_input_func=mock_input_no, db_service=mock_db)
    assert executed is False
    mock_db.execute.assert_not_called()

    # handle_run_command with 'no'
    executed_run = handle_run_command(state=state, user_input_func=mock_input_no, db_service=mock_db)
    assert executed_run is False
    mock_db.execute.assert_not_called()


# 16. Unbounded mutations are never silently introduced by the model without full-table scope
def test_unbounded_mutation_flags_high_impact(mutation_schema):
    sql = "DELETE FROM users;"
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_unrestricted is True
    assert res.mutation_scope == "FULL_TABLE"
    assert "Full-table" in res.status_message


def test_subquery_limit_on_foreign_key_does_not_falsely_claim_bounded_rows(mutation_schema):
    """Verify that LIMIT N in a subquery targeting a non-unique foreign key is classified as FILTERED, not BOUNDED."""
    sql = """
    DELETE FROM users
    WHERE org_id IN (
        SELECT id
        FROM organizations
        ORDER BY id
        LIMIT 5
    );
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    # Crucial safety check: It should NOT be classified as BOUNDED 5 rows, because multiple users can belong to each org
    assert res.is_bounded is False
    assert res.mutation_scope == "FILTERED"
    assert res.mutation_limit is None
    assert "Targeted DELETE with WHERE clause" in res.status_message


# ---------------------------------------------------------------------------
# ADVERSARIAL VERIFICATION SUITE
# ---------------------------------------------------------------------------

# Case A: Unrelated CTE
def test_adversarial_case_a_unrelated_cte(mutation_schema):
    """An unrelated CTE with a limit must NEVER classify the mutation as bounded."""
    sql = """
    WITH selected AS (
        SELECT id FROM students LIMIT 5
    )
    DELETE FROM users WHERE status = 'inactive';
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is False
    assert res.mutation_limit is None
    assert res.mutation_scope == "FILTERED"
    assert res.is_ordered is False
    assert res.selection_criteria == "status = 'inactive'"


# Case B: Non-unique target column
def test_adversarial_case_b_non_unique_target_column(mutation_schema):
    """Subquery limiting a non-unique column (email) must NOT be bounded unless verified unique."""
    sql = """
    DELETE FROM users
    WHERE email IN (
        SELECT email FROM users
        ORDER BY email LIMIT 5
    );
    """
    # 1. With standard mutation_schema where email is NOT marked unique
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is False
    assert res.mutation_limit is None
    assert res.mutation_scope == "FILTERED"

    # 2. When email IS verified unique in schema
    from support.schema.models import UniqueConstraintSchema
    users_with_unique_email = mutation_schema.tables["users"].model_copy(deep=True)
    users_with_unique_email.unique_constraints.append(
        UniqueConstraintSchema(name="uq_users_email", columns=["email"])
    )
    schema_with_unique = DatabaseSchema(
        dialect=Dialect.POSTGRES,
        tables={"users": users_with_unique_email, "orders": mutation_schema.tables["orders"]},
    )
    res_unique = validate_sql_query(sql, dialect="postgres", schema=schema_with_unique, allow_mutations=True)
    assert res_unique.is_valid is True
    assert res_unique.is_bounded is True
    assert res_unique.mutation_limit == 5
    assert res_unique.mutation_scope == "BOUNDED"
    assert res_unique.is_deterministic is True


# Case C: Target primary key
def test_adversarial_case_c_target_primary_key(mutation_schema):
    """Subquery limiting the target table's primary key must be recognized as bounded."""
    sql = """
    DELETE FROM users
    WHERE id IN (
        SELECT id FROM users LIMIT 5
    );
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is True
    assert res.mutation_limit == 5
    assert res.mutation_scope == "BOUNDED"


# Case D: Bounded but nondeterministic
def test_adversarial_case_d_bounded_but_nondeterministic(mutation_schema):
    """Distinguish row bound from selection determinism. It is bounded but does not guarantee which 5 rows are chosen."""
    sql = """
    DELETE FROM users
    WHERE id IN (
        SELECT id FROM users LIMIT 5
    );
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is True
    assert res.mutation_limit == 5
    assert res.is_ordered is False
    assert res.is_deterministic is False
    assert "Nondeterministic" in res.determinism_reason


# Case E: Ordered but not uniquely ordered
def test_adversarial_case_e_ordered_but_not_uniquely_ordered(mutation_schema):
    """Bounded and ordered, but lacks unique tie-breaker (timestamps may produce ties)."""
    sql = """
    DELETE FROM users
    WHERE id IN (
        SELECT id FROM users
        ORDER BY created_at
        LIMIT 5
    );
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is True
    assert res.mutation_limit == 5
    assert res.is_ordered is True
    assert res.is_deterministic is False
    assert "Partially ordered" in res.determinism_reason


# Case F: Ordered with unique tie-breaker
def test_adversarial_case_f_ordered_with_unique_tiebreaker(mutation_schema):
    """Both bounded and deterministically selected with verified primary key tie-breaker."""
    sql = """
    DELETE FROM users
    WHERE id IN (
        SELECT id FROM users
        ORDER BY created_at ASC, id ASC
        LIMIT 5
    );
    """
    res = validate_sql_query(sql, dialect="postgres", schema=mutation_schema, allow_mutations=True)
    assert res.is_valid is True
    assert res.is_bounded is True
    assert res.mutation_limit == 5
    assert res.is_ordered is True
    assert res.is_deterministic is True
    assert "Deterministic" in res.determinism_reason
    assert "id" in res.determinism_reason


