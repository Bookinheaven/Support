"""Tests for schema-qualified table inspection across dialects and live databases."""

from unittest.mock import MagicMock
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, mysql, sqlite, mssql

from support.core.dialect import Dialect
from support.db.models import ConnectionProfile
from support.db.service import DatabaseService
from support.query.executor import QueryResult, ReadOnlyQueryExecutor
from support.query.inspection import (
    MultiTableInspectionPlan,
    TableInspectionItem,
    build_inspection_plan,
    execute_multi_table_inspection,
)
from support.query.schema_context import inspect_database_schema
from support.query.validator import validate_sql_query
from support.schema.models import ColumnSchema, DatabaseSchema, TableSchema
from support.workspace.state import WorkspaceSessionState


@pytest.fixture
def multi_schema_database():
    """Create a database schema containing default and non-default schemas."""
    schema = DatabaseSchema(dialect=Dialect.POSTGRES)

    # 1. public.users
    schema.add_table(
        TableSchema(
            name="users",
            schema_name="public",
            columns=[
                ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
                ColumnSchema(name="username", data_type="VARCHAR(50)"),
                ColumnSchema(name="email", data_type="VARCHAR(100)"),
                ColumnSchema(name="password_hash", data_type="VARCHAR(255)"),
            ],
        )
    )

    # 2. drizzle.__drizzle_migrations (non-default schema)
    schema.add_table(
        TableSchema(
            name="__drizzle_migrations",
            schema_name="drizzle",
            columns=[
                ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
                ColumnSchema(name="hash", data_type="VARCHAR(64)"),
                ColumnSchema(name="created_at", data_type="BIGINT"),
            ],
        )
    )

    # 3. audit.users (table with identical name in a different schema)
    schema.add_table(
        TableSchema(
            name="users",
            schema_name="audit",
            columns=[
                ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
                ColumnSchema(name="action", data_type="VARCHAR(50)"),
                ColumnSchema(name="changed_at", data_type="TIMESTAMP"),
            ],
        )
    )

    # 4. Special characters: schema "analytics department", table "quarterly metrics"
    schema.add_table(
        TableSchema(
            name="quarterly metrics",
            schema_name="analytics department",
            columns=[
                ColumnSchema(name="metric id", data_type="INTEGER", is_primary_key=True),
                ColumnSchema(name="net revenue", data_type="NUMERIC"),
            ],
        )
    )

    return schema


def test_postgres_table_in_public_schema(multi_schema_database):
    """1. PostgreSQL tables in public are quoted as 'public'.'users', not 'public.users'."""
    profile = ConnectionProfile(
        name="pg_test",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="testdb",
        username="postgres",
    )
    plan = build_inspection_plan(profile, multi_schema_database, target_tables=["public.users"], row_limit=1)
    assert plan is not None
    assert len(plan.eligible_tables) == 1
    item = plan.eligible_tables[0]
    assert item.display_name == "public.users"
    assert item.table_name == "users"
    assert item.schema_name == "public"

    # Must contain "public"."users" and NOT "public.users"
    assert '"public"."users"' in item.sql
    assert '"public.users"' not in item.sql
    assert 'LIMIT 1' in item.sql


def test_postgres_table_in_non_default_schema(multi_schema_database):
    """2. PostgreSQL tables in a non-default schema such as drizzle are compiled properly."""
    profile = ConnectionProfile(
        name="pg_test",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="testdb",
        username="postgres",
    )
    plan = build_inspection_plan(
        profile, multi_schema_database, target_tables=["drizzle.__drizzle_migrations"], row_limit=1
    )
    assert plan is not None
    assert len(plan.eligible_tables) == 1
    item = plan.eligible_tables[0]
    assert item.display_name == "drizzle.__drizzle_migrations"
    assert '"drizzle"."__drizzle_migrations"' in item.sql
    assert '"drizzle.__drizzle_migrations"' not in item.sql


def test_schema_and_table_with_special_characters(multi_schema_database):
    """3. Schema and table name containing special characters (spaces) are quoted safely."""
    profile = ConnectionProfile(
        name="pg_test",
        dialect=Dialect.POSTGRES,
        host="localhost",
        port=5432,
        database="testdb",
        username="postgres",
    )
    plan = build_inspection_plan(
        profile, multi_schema_database, target_tables=["analytics department.quarterly metrics"], row_limit=1
    )
    assert plan is not None
    assert len(plan.eligible_tables) == 1
    item = plan.eligible_tables[0]
    assert '"analytics department"."quarterly metrics"' in item.sql
    assert '"metric id"' in item.sql
    assert '"net revenue"' in item.sql


def test_independent_quoting_of_schema_and_table_components():
    """4. Correct quoting of schema and table components independently across SQLAlchemy and SQLGlot."""
    # SQLAlchemy Core compiler verification
    metadata = sa.MetaData()
    pg_tbl = sa.Table("users", metadata, sa.Column("id", sa.Integer), schema="public", quote=True, quote_schema=True)
    pg_stmt = sa.select(pg_tbl.c.id).limit(1)
    compiled_pg = str(pg_stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert '"public"."users"' in compiled_pg
    assert '"public.users"' not in compiled_pg

    # SQLGlot AST verification
    import sqlglot.expressions as exp

    t_expr = exp.Table(
        this=exp.to_identifier("users", quoted=True),
        db=exp.to_identifier("public", quoted=True),
    )
    sql_pg = exp.select(exp.to_identifier("id", quoted=True)).from_(t_expr).limit(1).sql("postgres")
    assert '"public"."users"' in sql_pg
    assert '"public.users"' not in sql_pg


def test_tables_with_identical_names_in_different_schemas(multi_schema_database):
    """5. Tables with identical names in different schemas validate distinct columns without collision."""
    # public.users has 'email'
    q_public = 'SELECT "id", "email" FROM "public"."users" LIMIT 1;'
    res_pub = validate_sql_query(q_public, dialect=Dialect.POSTGRES, schema=multi_schema_database)
    assert res_pub.is_valid is True
    assert res_pub.is_schema_verified is True
    assert "public.users" in res_pub.schema_status

    # audit.users has 'action' and 'changed_at', but NOT 'email'
    q_audit = 'SELECT "id", "action", "changed_at" FROM "audit"."users" LIMIT 1;'
    res_audit = validate_sql_query(q_audit, dialect=Dialect.POSTGRES, schema=multi_schema_database)
    assert res_audit.is_valid is True
    assert res_audit.is_schema_verified is True
    assert "audit.users" in res_audit.schema_status

    # If someone queries audit.users for 'email', it should fail column verification
    q_audit_invalid = 'SELECT "email" FROM "audit"."users" LIMIT 1;'
    res_audit_inv = validate_sql_query(q_audit_invalid, dialect=Dialect.POSTGRES, schema=multi_schema_database)
    assert res_audit_inv.is_schema_verified is False


def test_sqlite_tables_unqualified_behavior():
    """6. SQLite tables preserve existing unqualified schema behavior."""
    sqlite_schema = DatabaseSchema(dialect=Dialect.SQLITE)
    sqlite_schema.add_table(
        TableSchema(
            name="notes",
            schema_name=None,
            columns=[ColumnSchema(name="id"), ColumnSchema(name="content")],
        )
    )
    profile = ConnectionProfile(
        name="local_sqlite",
        dialect=Dialect.SQLITE,
        database="local.db",
    )
    plan = build_inspection_plan(profile, sqlite_schema, target_tables=["notes"], row_limit=1)
    assert plan is not None
    item = plan.eligible_tables[0]
    assert item.display_name == "notes"
    assert item.schema_name is None
    assert '"notes"' in item.sql
    assert "." not in item.sql.split("FROM")[1].split("LIMIT")[0]


def test_mysql_and_sql_server_dialect_compilation(multi_schema_database):
    """7. MySQL and SQL Server compile schema-qualified identifiers with dialect-specific quotes and limits."""
    # MySQL
    mysql_prof = ConnectionProfile(
        name="mysql_db",
        dialect=Dialect.MYSQL,
        host="localhost",
        port=3306,
        database="testdb",
        username="root",
    )
    plan_my = build_inspection_plan(mysql_prof, multi_schema_database, target_tables=["public.users"], row_limit=1)
    assert plan_my is not None
    item_my = plan_my.eligible_tables[0]
    assert "`public`.`users`" in item_my.sql
    assert "LIMIT 1" in item_my.sql

    # SQL Server (tsql)
    mssql_prof = ConnectionProfile(
        name="mssql_db",
        dialect=Dialect.MSSQL,
        host="localhost",
        port=1433,
        database="testdb",
        username="sa",
    )
    plan_ms = build_inspection_plan(mssql_prof, multi_schema_database, target_tables=["public.users"], row_limit=1)
    assert plan_ms is not None
    item_ms = plan_ms.eligible_tables[0]
    assert "[public].[users]" in item_ms.sql
    assert "TOP 1" in item_ms.sql or "LIMIT 1" in item_ms.sql


def test_successful_table_sample(multi_schema_database):
    """8. A successful table sample returns records cleanly and records outcome."""
    profile = ConnectionProfile(
        name="pg_test", dialect=Dialect.POSTGRES, host="localhost", port=5432, database="testdb", username="postgres"
    )
    plan = build_inspection_plan(profile, multi_schema_database, target_tables=["public.users"], row_limit=1)
    state = WorkspaceSessionState(active_database="pg_test")
    state.cached_schema = multi_schema_database

    mock_exec = MagicMock(spec=ReadOnlyQueryExecutor)
    mock_exec.execute.return_value = QueryResult(
        row_count=1,
        columns=["id", "username", "email"],
        rows=[[1, "alice", "alice@example.com"]],
        duration_ms=15.0,
    )

    res = execute_multi_table_inspection(
        plan=plan,
        profile=profile,
        state=state,
        executor=mock_exec,
        user_input_func=lambda _: "y",
    )
    assert res is not None
    assert res.total_inspected == 1
    assert res.tables_with_rows == 1
    assert res.empty_tables == 0
    assert res.failed_tables == 0


def test_empty_table_reported_accurately(multi_schema_database):
    """9. An empty table (0 rows) is distinguished from execution errors."""
    profile = ConnectionProfile(
        name="pg_test", dialect=Dialect.POSTGRES, host="localhost", port=5432, database="testdb", username="postgres"
    )
    plan = build_inspection_plan(profile, multi_schema_database, target_tables=["public.users"], row_limit=1)
    state = WorkspaceSessionState(active_database="pg_test")
    state.cached_schema = multi_schema_database

    mock_exec = MagicMock(spec=ReadOnlyQueryExecutor)
    mock_exec.execute.return_value = QueryResult(
        row_count=0,
        columns=["id", "username", "email"],
        rows=[],
        duration_ms=8.0,
    )

    res = execute_multi_table_inspection(
        plan=plan,
        profile=profile,
        state=state,
        executor=mock_exec,
        user_input_func=lambda _: "y",
    )
    assert res is not None
    assert res.empty_tables == 1
    assert res.failed_tables == 0
    assert res.tables_with_rows == 0


def test_one_table_failing_continues_remaining_tables(multi_schema_database):
    """10. One table failing execution does not halt the inspection of subsequent tables."""
    profile = ConnectionProfile(
        name="pg_test", dialect=Dialect.POSTGRES, host="localhost", port=5432, database="testdb", username="postgres"
    )
    plan = build_inspection_plan(
        profile, multi_schema_database, target_tables=["public.users", "drizzle.__drizzle_migrations"], row_limit=1
    )
    state = WorkspaceSessionState(active_database="pg_test")
    state.cached_schema = multi_schema_database

    mock_exec = MagicMock(spec=ReadOnlyQueryExecutor)
    # First table fails (permission denied), second table succeeds
    mock_exec.execute.side_effect = [
        QueryResult(error_message="permission denied for relation users", duration_ms=5.0),
        QueryResult(row_count=1, columns=["id", "hash"], rows=[[1, "hash123"]], duration_ms=10.0),
    ]

    res = execute_multi_table_inspection(
        plan=plan,
        profile=profile,
        state=state,
        executor=mock_exec,
        user_input_func=lambda _: "y",
    )
    assert res is not None
    assert res.total_inspected == 2
    assert res.failed_tables == 1
    assert res.tables_with_rows == 1
    assert res.table_results[0].is_error is True
    assert res.table_results[1].is_error is False


def test_sensitive_column_exclusion_remains_intact(multi_schema_database):
    """11. Sensitive columns (password_hash) are strictly excluded from generated SQL."""
    profile = ConnectionProfile(
        name="pg_test", dialect=Dialect.POSTGRES, host="localhost", port=5432, database="testdb", username="postgres"
    )
    plan = build_inspection_plan(profile, multi_schema_database, target_tables=["public.users"], row_limit=1)
    assert plan is not None
    item = plan.eligible_tables[0]
    assert "password_hash" not in item.sql
    assert "password_hash" in item.sensitive_columns_excluded
    assert "username" in item.columns


def test_no_credentials_leaked_through_execution_errors(multi_schema_database):
    """12. No passwords or credentials leak through error reporting."""
    profile = ConnectionProfile(
        name="pg_test", dialect=Dialect.POSTGRES, host="localhost", port=5432, database="testdb", username="postgres"
    )
    plan = build_inspection_plan(profile, multi_schema_database, target_tables=["public.users"], row_limit=1)
    state = WorkspaceSessionState(active_database="pg_test")
    state.cached_schema = multi_schema_database

    mock_exec = MagicMock(spec=ReadOnlyQueryExecutor)
    mock_exec.execute.return_value = QueryResult(
        error_message="FATAL: password authentication failed for user postgres with secret_pass_1234",
        duration_ms=10.0,
    )

    mock_db = MagicMock()
    mock_db.manager.get_profile_with_password.return_value = (profile, "secret_pass_1234")

    res = execute_multi_table_inspection(
        plan=plan,
        profile=profile,
        state=state,
        db_service=mock_db,
        executor=mock_exec,
        user_input_func=lambda _: "y",
    )
    assert res is not None
    assert res.failed_tables == 1
    assert "secret_pass_1234" not in res.table_results[0].error_message
    assert "***" in res.table_results[0].error_message


def test_live_postgresql_schema_qualified_table_inspection():
    """13. Live database integration test against real PostgreSQL 'athena' instance if online."""
    dbs = DatabaseService()
    try:
        prof, pw = dbs.manager.get_profile_with_password("athena")
    except Exception:
        pytest.skip("Profile 'athena' not configured in local environment.")

    res_test = dbs.test_profile("athena")
    if not res_test[1].is_online:
        pytest.skip(f"Live PostgreSQL 'athena' is offline ({res_test[1].error_message}).")

    schema = inspect_database_schema(prof)
    assert "public.users" in schema.tables
    assert "drizzle.__drizzle_migrations" in schema.tables

    # Build plan for live PostgreSQL
    plan = build_inspection_plan(
        prof, schema, target_tables=["public.users", "drizzle.__drizzle_migrations"], row_limit=1
    )
    assert plan is not None
    assert len(plan.eligible_tables) == 2

    # Verify SQL syntax
    users_item = [t for t in plan.eligible_tables if t.display_name == "public.users"][0]
    drizzle_item = [t for t in plan.eligible_tables if t.display_name == "drizzle.__drizzle_migrations"][0]
    assert '"public"."users"' in users_item.sql
    assert '"drizzle"."__drizzle_migrations"' in drizzle_item.sql

    # Execute against live PostgreSQL
    state = WorkspaceSessionState(active_database=prof.name)
    state.cached_schema = schema
    res = execute_multi_table_inspection(
        plan=plan,
        profile=prof,
        state=state,
        db_service=dbs,
        user_input_func=lambda _: "y",
        timeout_seconds=5.0,
    )
    assert res is not None
    assert res.total_inspected == 2
    assert res.failed_tables == 0
    assert res.tables_with_rows == 2
    assert res.total_rows == 2
