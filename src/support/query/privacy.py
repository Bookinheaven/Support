"""Centralized sensitive data protection, column classification, and redaction policies."""

import re
from typing import Any, Optional, Union

import sqlglot
from sqlglot import exp

from support.core.dialect import Dialect
from support.schema.models import DatabaseSchema, TableSchema

# Centralized, conservative regex patterns identifying sensitive credentials and tokens
SENSITIVE_PATTERNS = [
    r"^pass(word|wd|phrase)?(_hash|_digest)?$",
    r"^pwd(_hash)?$",
    r"^salt$",
    r"^(auth|access|refresh|session|id|bearer|csrf|reset|verification)_?token$",
    r"token",
    r"api_?key",
    r"apikey",
    r"secret(_?key)?",
    r"(client|app|user)_?secret",
    r"priv(ate)?_?key",
    r"credential(s)?",
    r"cookie",
    r"session_?id",
    r"ssn",
    r"credit_?card",
    r"cvv",
]

_COMPILED_SENSITIVE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]


def is_sensitive_column_name(col_name: str) -> bool:
    """Return True if a column name indicates sensitive credential, authentication, or secret data."""
    if not col_name:
        return False
    clean = col_name.strip().lower()
    return any(pattern.search(clean) for pattern in _COMPILED_SENSITIVE_PATTERNS)


def get_table_sensitive_columns(table: TableSchema) -> list[str]:
    """Retrieve list of column names in a table that match sensitive data criteria."""
    return [col.name for col in table.columns if is_sensitive_column_name(col.name)]


def get_schema_sensitive_columns(schema: DatabaseSchema) -> dict[str, list[str]]:
    """Map table names in schema to their respective sensitive column names."""
    result: dict[str, list[str]] = {}
    for t_name, table in schema.tables.items():
        sens = get_table_sensitive_columns(table)
        if sens:
            result[t_name] = sens
            short = t_name.split(".")[-1]
            result[short] = sens
    return result


def identify_sensitive_projections(
    stmt: exp.Expression,
    schema: Optional[DatabaseSchema] = None,
) -> list[str]:
    """Analyze AST projections to identify sensitive output columns, including aliased sources."""
    sensitive_found: list[str] = []

    # Map of known table columns if schema is present
    schema_sensitive_cols = set()
    if schema:
        for table in schema.tables.values():
            for c in table.columns:
                if is_sensitive_column_name(c.name):
                    schema_sensitive_cols.add(c.name.lower())

    for sel in stmt.selects:
        output_name = sel.output_name.lower() if sel.output_name else ""
        if is_sensitive_column_name(output_name):
            sensitive_found.append(sel.output_name)
            continue

        # Inspect underlying Column expressions behind any aliases
        for col in sel.find_all(exp.Column):
            c_name = col.name.lower()
            if is_sensitive_column_name(c_name) or c_name in schema_sensitive_cols:
                sensitive_found.append(sel.output_name or col.name)
                break

    return sorted(list(set(sensitive_found)))


def has_sensitive_wildcard(
    stmt: exp.Expression,
    schema: Optional[DatabaseSchema] = None,
) -> tuple[bool, list[str]]:
    """Check if query contains wildcard projections on tables containing sensitive fields."""
    has_star = any(isinstance(sel, exp.Star) or sel.find(exp.Star) is not None for sel in stmt.selects)
    if not has_star:
        return False, []

    if not schema:
        # Without schema metadata, wildcards are potentially unsafe
        return True, ["unknown_tables"]

    sensitive_tables_found: list[str] = []
    for table_node in stmt.find_all(exp.Table):
        t_name = table_node.name.lower()
        # Check against schema tables
        matched_table = None
        for k, tbl in schema.tables.items():
            if k.lower() == t_name or tbl.name.lower() == t_name or k.split(".")[-1].lower() == t_name:
                matched_table = tbl
                break

        if matched_table:
            sens_cols = get_table_sensitive_columns(matched_table)
            if sens_cols:
                sensitive_tables_found.append(matched_table.name)
        elif is_sensitive_column_name(t_name) or t_name in ("users", "accounts", "credentials", "tokens"):
            sensitive_tables_found.append(table_node.name)

    return len(sensitive_tables_found) > 0, sensitive_tables_found


def try_expand_wildcard_projections(
    stmt: exp.Expression,
    schema: DatabaseSchema,
    dialect_str: str = "postgres",
) -> Optional[exp.Expression]:
    """Deterministically transform SELECT * into explicit non-sensitive columns if schema permits."""
    # Only transform SELECT queries containing a wildcard projection
    if not isinstance(stmt, (exp.Select, exp.Union)) or not stmt.find(exp.Star):
        return None

    # Only transform simple single-table queries or clear projections
    tables = list(stmt.find_all(exp.Table))
    if len(tables) != 1:
        return None

    target_table_node = tables[0]
    t_name = target_table_node.name.lower()

    matched_table: Optional[TableSchema] = None
    for k, tbl in schema.tables.items():
        if k.lower() == t_name or tbl.name.lower() == t_name or k.split(".")[-1].lower() == t_name:
            matched_table = tbl
            break

    if not matched_table:
        return None

    # Compute permitted non-sensitive columns in original schema order
    permitted_cols = [c.name for c in matched_table.columns if not is_sensitive_column_name(c.name)]
    if not permitted_cols:
        return None

    alias = target_table_node.alias_or_name
    use_alias = alias and alias != matched_table.name

    new_stmt = stmt.copy()
    new_projections = [
        exp.column(c, table=alias if use_alias else None)
        for c in permitted_cols
    ]
    new_stmt.set("expressions", new_projections)
    return new_stmt


def redact_sensitive_result_rows(
    columns: list[str],
    rows: list[list[Any]],
    sql: Optional[str] = None,
    dialect: Optional[Union[Dialect, str]] = None,
    schema: Optional[DatabaseSchema] = None,
) -> tuple[list[list[Any]], list[str]]:
    """Redact sensitive fields in execution results as a defense-in-depth safeguard.

    Returns:
        (redacted_rows, list_of_redacted_column_names)
    """
    if not rows or not columns:
        return rows, []

    sensitive_indices: set[int] = set()
    redacted_col_names: list[str] = []

    # 1. Direct column name check
    for idx, col in enumerate(columns):
        if is_sensitive_column_name(col):
            sensitive_indices.add(idx)
            redacted_col_names.append(col)

    # 2. Lineage AST inspection if SQL is provided
    if sql:
        try:
            read_dialect = "postgres"
            if dialect:
                read_dialect = dialect.value if isinstance(dialect, Dialect) else str(dialect)
            parsed = sqlglot.parse_one(sql, read=read_dialect)
            ast_sensitive = set(identify_sensitive_projections(parsed, schema=schema))
            for idx, col in enumerate(columns):
                if col in ast_sensitive or col.lower() in {s.lower() for s in ast_sensitive}:
                    sensitive_indices.add(idx)
                    if col not in redacted_col_names:
                        redacted_col_names.append(col)
        except Exception:
            pass

    if not sensitive_indices:
        return rows, []

    # Produce redacted copy of rows without mutating input
    redacted_rows: list[list[Any]] = []
    for row in rows:
        new_row = list(row)
        for idx in sensitive_indices:
            if idx < len(new_row):
                new_row[idx] = "[REDACTED]"
        redacted_rows.append(new_row)

    return redacted_rows, sorted(redacted_col_names)
