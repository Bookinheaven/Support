"""Dialect-aware SQL query validator enforcing read-only safety and schema alignment via AST inspection."""

from dataclasses import dataclass, field
import re
from typing import Optional, Union

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from support.core.dialect import Dialect
from support.schema.models import DatabaseSchema


# SQLGlot AST node types that mutate state, schema, file system, or locking configuration
PROHIBITED_EXPRESSIONS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    getattr(exp, "TruncateTable", exp.Expression),
    exp.Command,
    exp.Set,
    exp.Kill,
    exp.Pragma,
    exp.Into,   # SELECT INTO table
    exp.Lock,   # FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE
)

# T-SQL table hints that lock rows or tables
LOCKING_TABLE_HINTS = {
    "updlock",
    "xlock",
    "holdlock",
    "tablock",
    "tablockx",
    "paglock",
    "rowlock",
}

DIALECT_MAPPING = {
    Dialect.POSTGRES: "postgres",
    Dialect.MYSQL: "mysql",
    Dialect.SQLITE: "sqlite",
    Dialect.MSSQL: "tsql",
}


@dataclass
class ValidationResult:
    """Detailed result of SQL parsing, read-only AST verification, and schema element check.

    Static AST validation proves that the statement is structurally a single read-only
    SELECT/UNION query and contains no data-modifying, file-export, or locking AST nodes.
    It does not execute the query, and cannot verify remote database-side behavior of
    custom user-defined functions or stored procedures.
    """

    is_valid: bool
    status_message: str
    is_parsed: bool = True
    is_readonly: bool = True
    tables_referenced: list[str] = field(default_factory=list)
    columns_referenced: list[str] = field(default_factory=list)
    unknown_tables: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    is_schema_verified: Optional[bool] = None
    schema_status: str = "Not checked"

    def __iter__(self):
        """Allow backwards-compatible tuple unpacking: is_valid, msg = validate_sql_query(...)."""
        yield self.is_valid
        yield self.status_message


def _resolve_sqlglot_dialect(dialect: Union[Dialect, str]) -> str:
    """Normalize dialect enum or string to a valid SQLGlot dialect identifier."""
    if isinstance(dialect, Dialect):
        return DIALECT_MAPPING.get(dialect, "postgres")

    clean = dialect.strip().lower()
    if clean in ("postgres", "postgresql", "pgsql"):
        return "postgres"
    elif clean in ("mysql", "mariadb"):
        return "mysql"
    elif clean in ("sqlite", "sqlite3"):
        return "sqlite"
    elif clean in ("mssql", "sqlserver", "tsql"):
        return "tsql"
    return "postgres"


def is_system_metadata_table_node(tbl: exp.Table) -> bool:
    """Identify standard SQL dialect-aware system metadata catalogs and views."""
    t_name = tbl.name.lower()
    db_name = tbl.db.lower() if tbl.db else ""

    # 1. information_schema standard views (PostgreSQL, MySQL, SQL Server)
    if db_name == "information_schema":
        return t_name in {
            "tables", "columns", "schemata", "views", "table_constraints",
            "key_column_usage", "referential_constraints", "routines",
            "parameters", "statistics", "check_constraints", "domains",
        }

    # 2. PostgreSQL pg_catalog
    if db_name == "pg_catalog" or t_name.startswith("pg_"):
        return t_name in {
            "pg_tables", "pg_class", "pg_namespace", "pg_attribute",
            "pg_type", "pg_index", "pg_database", "pg_views", "pg_indexes",
            "pg_stat_user_tables", "pg_statio_user_tables", "pg_roles", "pg_user",
        }

    # 3. SQLite system tables and schema pragmas
    if t_name in {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}:
        return True
    if t_name.startswith("pragma_"):
        return True

    # 4. SQL Server sys views
    if db_name == "sys":
        return t_name in {
            "tables", "columns", "schemas", "objects", "databases",
            "indexes", "foreign_keys", "foreign_key_columns", "types",
        }

    return False


def check_schema_alignment(
    stmt: exp.Expression,
    schema: Optional[DatabaseSchema] = None,
) -> tuple[list[str], list[str], list[str], list[str], Optional[bool], str]:
    """Inspect tables and columns referenced in AST against discovered database schema."""
    if schema is None:
        return [], [], [], [], None, "Schema not available for verification"

    # 1. Identify local CTE aliases so they are not treated as missing tables
    cte_names = set()
    for cte in stmt.find_all(exp.CTE):
        if cte.alias:
            cte_names.add(cte.alias.lower())

    # 2. Extract external tables and system metadata tables referenced
    tables_ref: set[str] = set()
    system_metadata_tables: set[str] = set()
    unknown_tables: list[str] = []

    known_tables = {t.name.lower(): t for t in schema.tables.values()}
    known_short_names = {t.name.split(".")[-1].lower(): t for t in schema.tables.values()}

    for tbl in stmt.find_all(exp.Table):
        if not tbl.name:
            continue
        t_name = tbl.name.lower()
        if t_name in cte_names:
            continue

        tables_ref.add(tbl.name)
        if is_system_metadata_table_node(tbl):
            system_metadata_tables.add(t_name)
        elif t_name not in known_tables and t_name not in known_short_names:
            unknown_tables.append(tbl.name)

    unknown_tables = sorted(list(set(unknown_tables)))

    # 3. Extract columns referenced
    cols_ref: set[str] = set()
    for col_expr in stmt.find_all(exp.Column):
        if col_expr.name and col_expr.name != "*":
            cols_ref.add(col_expr.name.lower())

    # Check unknown columns against application tables only
    unknown_cols = []
    app_tables_ref = [t.lower() for t in tables_ref if t.lower() not in system_metadata_tables]
    if not unknown_tables and app_tables_ref:
        ref_table_columns = set()
        for t_name in app_tables_ref:
            table_obj = known_tables.get(t_name) or known_short_names.get(t_name)
            if table_obj:
                for c in table_obj.columns:
                    ref_table_columns.add(c.name.lower())

        if ref_table_columns:
            for c_name in sorted(cols_ref):
                if c_name not in ref_table_columns and not cte_names:
                    unknown_cols.append(c_name)

    if unknown_tables:
        is_matched = False
        status_msg = f"Warning: References tables not in schema: {', '.join(unknown_tables)}"
    elif unknown_cols:
        is_matched = False
        status_msg = f"Warning: References unknown column(s): {', '.join(unknown_cols)}"
    elif tables_ref:
        is_matched = True
        status_msg = f"Verified: References {len(tables_ref)} known table(s) ({', '.join(sorted(tables_ref))})"
    else:
        is_matched = True
        status_msg = "Verified: Standalone query without table references"

    return (
        sorted(tables_ref),
        sorted(cols_ref),
        unknown_tables,
        unknown_cols,
        is_matched,
        status_msg,
    )


def validate_sql_query(
    sql: str,
    dialect: Union[Dialect, str] = Dialect.POSTGRES,
    schema: Optional[DatabaseSchema] = None,
) -> ValidationResult:
    """Validate that the generated SQL query is a single, valid, read-only statement.

    Args:
        sql: The SQL query string to validate.
        dialect: The target database dialect.
        schema: Optional discovered DatabaseSchema to verify table and column references.

    Returns:
        A ValidationResult detailing parsing, read-only AST validity, and schema alignment.
    """
    cleaned_sql = sql.strip()
    if not cleaned_sql:
        return ValidationResult(
            is_valid=False,
            is_parsed=False,
            is_readonly=False,
            status_message="Rejected: Query is empty",
        )

    # Pre-parse check: Reject MySQL file export clauses explicitly
    if re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", cleaned_sql, re.IGNORECASE):
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=False,
            status_message="Rejected: File export clauses ('INTO OUTFILE' / 'INTO DUMPFILE') are not permitted in read-only queries",
        )

    sqlglot_dialect = _resolve_sqlglot_dialect(dialect)

    try:
        statements = sqlglot.parse(cleaned_sql, read=sqlglot_dialect)
    except ParseError as e:
        return ValidationResult(
            is_valid=False,
            is_parsed=False,
            is_readonly=False,
            status_message=f"Rejected: SQL syntax error: {e}",
        )
    except Exception as e:
        return ValidationResult(
            is_valid=False,
            is_parsed=False,
            is_readonly=False,
            status_message=f"Rejected: Parse error: {e}",
        )

    # Filter out empty statements (e.g. trailing semicolons)
    valid_stmts = [s for s in statements if s is not None]

    if not valid_stmts:
        return ValidationResult(
            is_valid=False,
            is_parsed=False,
            is_readonly=False,
            status_message="Rejected: No valid SQL statement found",
        )

    if len(valid_stmts) > 1:
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=False,
            status_message=f"Rejected: Multiple SQL statements are not permitted (found {len(valid_stmts)} statements)",
        )

    stmt = valid_stmts[0]

    # Statement must be read-only query (Select or Union)
    if not isinstance(stmt, (exp.Select, exp.Union)):
        op_name = type(stmt).__name__
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=False,
            status_message=f"Rejected: Only read-only SELECT queries are permitted (found {op_name})",
        )

    # Check for SELECT ... INTO clause creating tables
    if stmt.find(exp.Into):
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=False,
            status_message="Rejected: Table creation/modification clause ('SELECT INTO') is not permitted in read-only queries",
        )

    # Check for row/table locking clauses (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE)
    if stmt.find(exp.Lock):
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=False,
            status_message="Rejected: Row/table locking clauses ('FOR UPDATE' / 'FOR SHARE' / 'LOCK IN') are not permitted in read-only queries",
        )

    # Check for T-SQL locking hints (UPDLOCK, XLOCK, HOLDLOCK, TABLOCKX, etc.)
    for hint in stmt.find_all(exp.WithTableHint):
        for hint_expr in hint.expressions:
            h_name = getattr(hint_expr, "name", str(hint_expr)).lower()
            if h_name in LOCKING_TABLE_HINTS:
                return ValidationResult(
                    is_valid=False,
                    is_parsed=True,
                    is_readonly=False,
                    status_message=f"Rejected: Concurrency locking hint '{h_name.upper()}' is not permitted in read-only queries",
                )

    # Recursively check the entire AST for any prohibited modification expressions
    for prohibited_cls in PROHIBITED_EXPRESSIONS:
        found_mutations = list(stmt.find_all(prohibited_cls))
        if found_mutations:
            op_name = prohibited_cls.__name__
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=False,
                status_message=f"Rejected: Prohibited operation detected in query: {op_name}",
            )

    # Check for direct or aliased sensitive field projections
    from support.query.privacy import (
        identify_sensitive_projections,
        has_sensitive_wildcard,
    )

    sensitive_projections = identify_sensitive_projections(stmt, schema=schema)
    if sensitive_projections:
        sens_str = ", ".join(sensitive_projections)
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=True,
            status_message=f"Rejected: Query projects sensitive credential/authentication field(s): {sens_str}",
        )

    # Check for unconstrained wildcard projections on tables containing sensitive fields
    has_star, sens_tables = has_sensitive_wildcard(stmt, schema=schema)
    if has_star and sens_tables and sens_tables != ["unknown_tables"]:
        t_str = ", ".join(sens_tables)
        return ValidationResult(
            is_valid=False,
            is_parsed=True,
            is_readonly=True,
            status_message=(
                f"Rejected: Wildcard projection 'SELECT *' is prohibited on tables containing sensitive fields "
                f"({t_str}). Please specify explicit non-sensitive columns."
            ),
        )

    # Check schema alignment if schema is provided
    (
        tables_ref,
        cols_ref,
        unknown_tables,
        unknown_cols,
        is_schema_ver,
        schema_msg,
    ) = check_schema_alignment(stmt, schema=schema)

    return ValidationResult(
        is_valid=True,
        is_parsed=True,
        is_readonly=True,
        status_message="Passed",
        tables_referenced=tables_ref,
        columns_referenced=cols_ref,
        unknown_tables=unknown_tables,
        unknown_columns=unknown_cols,
        is_schema_verified=is_schema_ver,
        schema_status=schema_msg,
    )
