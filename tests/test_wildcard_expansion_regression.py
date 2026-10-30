"""Regression test suite for precise wildcard expansion in support.query.privacy."""

import pytest
import sqlglot
from sqlglot import exp

from support.core.dialect import Dialect
from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
)
from support.query.privacy import (
    try_expand_wildcard_projections,
    is_sensitive_column_name,
)


def _build_test_schema() -> DatabaseSchema:
    schema = DatabaseSchema(dialect=Dialect.POSTGRES)

    users = TableSchema(
        name="users",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="org_id", data_type="INTEGER"),
            ColumnSchema(name="email", data_type="VARCHAR", nullable=False),
            ColumnSchema(name="full_name", data_type="VARCHAR"),
            ColumnSchema(name="password_hash", data_type="VARCHAR"),  # SENSITIVE
            ColumnSchema(name="api_token", data_type="VARCHAR"),      # SENSITIVE
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        primary_key=["id"],
    )

    orders = TableSchema(
        name="orders",
        columns=[
            ColumnSchema(name="id", data_type="INTEGER", is_primary_key=True),
            ColumnSchema(name="user_id", data_type="INTEGER"),
            ColumnSchema(name="total_amount", data_type="NUMERIC"),
            ColumnSchema(name="status", data_type="VARCHAR"),
            ColumnSchema(name="created_at", data_type="TIMESTAMP"),
        ],
        primary_key=["id"],
        foreign_keys=[
            ForeignKeySchema(constrained_columns=["user_id"], referred_table="users", referred_columns=["id"])
        ],
    )

    schema.tables["users"] = users
    schema.tables["orders"] = orders
    return schema


# 1. SELECT * FROM users expands correctly
def test_select_star_from_users_expands_correctly():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT * FROM users;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is not None

    col_names = [e.name for e in expanded.expressions]
    # Non-sensitive columns must be present in schema order
    assert col_names == ["id", "org_id", "email", "full_name", "created_at"]
    # Sensitive columns must NOT be present
    assert "password_hash" not in col_names
    assert "api_token" not in col_names


# 2. SELECT COUNT(*) FROM users preserves COUNT
def test_select_count_star_preserves_count():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT COUNT(*) FROM users;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    # Must NOT expand COUNT(*)
    assert expanded is None
    # Verify AST expression remains an instance of exp.Count
    assert isinstance(parsed.expressions[0], exp.Count)


# 3. SELECT COUNT(DISTINCT user_id) FROM orders remains unchanged
def test_select_count_distinct_remains_unchanged():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT COUNT(DISTINCT user_id) FROM orders;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is None
    assert isinstance(parsed.expressions[0], exp.Count)


# 4. SELECT SUM(total_amount) FROM orders remains unchanged
def test_select_sum_remains_unchanged():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT SUM(total_amount) FROM orders;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is None
    assert isinstance(parsed.expressions[0], exp.Sum)


# 5. SELECT status, COUNT(*) FROM orders GROUP BY status remains unchanged
def test_select_status_count_group_by_remains_unchanged():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT status, COUNT(*) FROM orders GROUP BY status;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is None
    # Projections must remain status and COUNT(*)
    assert len(parsed.expressions) == 2
    assert isinstance(parsed.expressions[0], exp.Column)
    assert isinstance(parsed.expressions[1], exp.Count)


# 6. Grouped queries containing HAVING remain unchanged
def test_grouped_queries_with_having_remain_unchanged():
    schema = _build_test_schema()
    sql = "SELECT status, SUM(total_amount) FROM orders GROUP BY status HAVING SUM(total_amount) > 100;"
    parsed = sqlglot.parse_one(sql, read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is None
    assert parsed.find(exp.Having) is not None


# 7. SELECT * ... GROUP BY ... is not incorrectly expanded
def test_select_star_group_by_is_not_expanded():
    schema = _build_test_schema()
    sql = "SELECT * FROM orders GROUP BY status;"
    parsed = sqlglot.parse_one(sql, read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    # Must NOT expand into individual unaggregated table columns
    assert expanded is None


# 8. SELECT users.* FROM users is handled correctly
def test_select_table_dot_star_is_handled_correctly():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT users.* FROM users;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is not None

    col_names = [e.name for e in expanded.expressions]
    assert col_names == ["id", "org_id", "email", "full_name", "created_at"]
    assert "password_hash" not in col_names


# 9. Joined queries with COUNT(*) preserve their joins and aggregation
def test_joined_queries_with_count_star_preserve_aggregation():
    schema = _build_test_schema()
    sql = "SELECT COUNT(*) FROM orders o JOIN users u ON o.user_id = u.id;"
    parsed = sqlglot.parse_one(sql, read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is None
    assert isinstance(parsed.expressions[0], exp.Count)
    assert len(list(parsed.find_all(exp.Join))) == 1


# 10. Existing privacy protections still work when expanding genuine wildcard projections
def test_existing_privacy_protections_exclude_sensitive_columns():
    schema = _build_test_schema()
    parsed = sqlglot.parse_one("SELECT * FROM users u;", read="postgres")
    expanded = try_expand_wildcard_projections(parsed, schema=schema, dialect_str="postgres")
    assert expanded is not None

    for expr in expanded.expressions:
        col_name = expr.name
        # Verify no sensitive columns leaked
        assert not is_sensitive_column_name(col_name)
        assert col_name not in ("password_hash", "api_token")

    # Verify alias table reference is preserved
    assert all(expr.table == "u" for expr in expanded.expressions)
