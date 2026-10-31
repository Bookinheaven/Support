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
    operation: str = "SELECT"
    tables_referenced: list[str] = field(default_factory=list)
    columns_referenced: list[str] = field(default_factory=list)
    unknown_tables: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    is_schema_verified: Optional[bool] = None
    schema_status: str = "Not checked"
    mutation_scope: str = "READ_ONLY"
    mutation_limit: Optional[int] = None
    is_bounded: bool = False
    is_deterministic: bool = False
    determinism_reason: Optional[str] = None
    is_unrestricted: bool = False
    selection_criteria: Optional[str] = None
    target_table: Optional[str] = None
    is_ordered: bool = False

    def __iter__(self):
        """Allow backwards-compatible tuple unpacking: is_valid, msg = validate_sql_query(...)."""
        yield self.is_valid
        yield self.status_message


@dataclass
class MutationScope:
    """Detailed scope and safety classification of a mutation statement."""

    operation: str
    target_table: Optional[str] = None
    scope_type: str = "READ_ONLY"  # "READ_ONLY", "FILTERED", "BOUNDED", "BOUNDED_FILTERED", "FULL_TABLE", "INVALID_DIALECT_LIMIT"
    is_bounded: bool = False
    limit_count: Optional[int] = None
    is_deterministic: bool = False
    is_ordered: bool = False
    determinism_reason: Optional[str] = None
    selection_criteria: Optional[str] = None
    is_unrestricted: bool = False
    is_dialect_valid: bool = True
    dialect_error: Optional[str] = None


def _is_column_unique_or_pk(col_expr: Any, target_table: Optional[str], schema: Optional[DatabaseSchema]) -> bool:
    """Verify whether a column expression corresponds to a primary key or unique column on the target table."""
    if not col_expr or not target_table:
        return False
    if hasattr(col_expr, "name"):
        col_name = col_expr.name
    elif isinstance(col_expr, str):
        col_name = col_expr
    else:
        col_name = str(col_expr)

    col_name = col_name.strip("`'\"[]").lower()
    if "." in col_name:
        col_name = col_name.split(".")[-1]

    if schema and hasattr(schema, "tables") and schema.tables:
        for t_name, t_val in schema.tables.items():
            if t_name.lower() == target_table.lower() or t_name.split(".")[-1].lower() == target_table.lower():
                pks = [pk.lower() for pk in getattr(t_val, "primary_key", [])]
                for col in getattr(t_val, "columns", []):
                    if getattr(col, "is_primary_key", False):
                        pks.append(col.name.lower())
                uniques = []
                for uq in getattr(t_val, "unique_constraints", []):
                    cols = getattr(uq, "columns", [])
                    if len(cols) == 1:
                        uniques.append(cols[0].lower())
                return (col_name in pks) or (col_name in uniques)
        return False

    # Heuristic fallback only when schema is not available:
    clean_target = target_table.lower()
    singular = clean_target[:-1] if clean_target.endswith("s") else clean_target
    return col_name == "id" or col_name == f"{singular}_id"


def _has_unique_order_tiebreaker(
    order_node: Optional[exp.Order],
    target_table: Optional[str],
    schema: Optional[DatabaseSchema],
) -> tuple[bool, Optional[str]]:
    """Verify whether an ORDER BY clause contains at least one unique column or primary key.

    Returns (is_deterministic, tiebreaker_column_name).
    """
    if not order_node or not target_table:
        return False, None

    for ordered_expr in order_node.expressions:
        for col in ordered_expr.find_all(exp.Column):
            if _is_column_unique_or_pk(col.name, target_table, schema):
                return True, col.name

    return False, None


def analyze_mutation_scope(
    stmt: exp.Expression,
    dialect_str: str = "postgres",
    schema: Optional[DatabaseSchema] = None,
) -> MutationScope:
    """Analyze AST of a mutation statement (DELETE / UPDATE) to determine affected scope and dialect validity."""
    op = "UPDATE" if isinstance(stmt, exp.Update) else ("DELETE" if isinstance(stmt, exp.Delete) else type(stmt).__name__.upper())

    # 1. Target table detection
    target_table = None
    if isinstance(stmt, (exp.Delete, exp.Update)):
        if stmt.this and isinstance(stmt.this, exp.Table):
            target_table = stmt.this.name
        elif stmt.find(exp.Table):
            target_table = stmt.find(exp.Table).name

    # Check if target is a CTE alias (e.g. WITH cte AS (...) DELETE FROM cte)
    cte_target_tbl = None
    with_node = stmt.find(exp.With)
    if with_node and target_table:
        for cte in with_node.expressions:
            alias_name = cte.alias_or_name
            if alias_name and alias_name.lower() == target_table.lower():
                inner_tbl = cte.find(exp.Table)
                if inner_tbl:
                    cte_target_tbl = inner_tbl.name

    effective_target_table = cte_target_tbl or target_table

    # Extract tables referenced in the actual mutation body (strictly excluding CTE definitions)
    copy_stmt = stmt.copy()
    copy_stmt.set("with_", None)
    mutation_body_tables = {t.name.lower() for t in copy_stmt.find_all(exp.Table) if t.name}

    # 2. WHERE clause analysis
    where_node = stmt.find(exp.Where)
    selection_criteria = None
    if where_node:
        selection_criteria = where_node.this.sql(dialect=dialect_str)

    # 3. Limit, bounding, and determinism analysis
    direct_limit_count = None
    in_subquery_limit_count = None
    cte_limit_count = None

    bounding_order_node: Optional[exp.Order] = None

    # Check direct limit on root statement
    direct_limit = stmt.args.get("limit")
    if direct_limit:
        try:
            lim_expr = direct_limit.expression
            val = lim_expr.this if hasattr(lim_expr, "this") else str(lim_expr)
            direct_limit_count = int(val)
        except (ValueError, TypeError, AttributeError):
            direct_limit_count = None

    # Check direct T-SQL TOP on root statement
    for tbl_node in stmt.find_all(exp.Table):
        if tbl_node.name.upper() == "TOP" and tbl_node.alias and tbl_node.alias.columns:
            try:
                direct_limit_count = int(tbl_node.alias.columns[0].name)
            except (ValueError, TypeError, IndexError):
                pass

    # Dialect validation on direct LIMIT:
    # MySQL natively supports direct LIMIT on DELETE and UPDATE.
    # PostgreSQL, SQLite, and SQL Server do NOT support direct LIMIT on DELETE or UPDATE.
    is_dialect_valid = True
    dialect_error = None
    clean_d = dialect_str.lower()
    if direct_limit is not None and clean_d in ("postgres", "postgresql", "pgsql", "sqlite", "sqlite3", "tsql", "mssql", "sqlserver"):
        is_dialect_valid = False
        display_d = "PostgreSQL" if "post" in clean_d or "pg" in clean_d else ("SQLite" if "sqlite" in clean_d else "SQL Server (T-SQL)")
        dialect_error = f"Direct LIMIT clause on {op} is not supported in {display_d}. In {display_d}, use a subquery ('WHERE id IN (SELECT id FROM ... LIMIT ...)') or a CTE instead."

    if is_dialect_valid and direct_limit_count is not None:
        bounding_order_node = copy_stmt.find(exp.Order)

    # Check bounding in subqueries within WHERE clause
    has_subquery_where = False
    if where_node:
        for in_expr in where_node.find_all(exp.In):
            sub = in_expr.args.get("query")
            if sub:
                if sub.find(exp.Where):
                    has_subquery_where = True
                sub_lim = sub.find(exp.Limit)
                if sub_lim:
                    col_bounds_target = _is_column_unique_or_pk(in_expr.this, effective_target_table, schema)
                    if col_bounds_target:
                        try:
                            lim_val = sub_lim.expression.this if hasattr(sub_lim.expression, "this") else str(sub_lim.expression)
                            in_subquery_limit_count = int(lim_val)
                            bounding_order_node = sub.find(exp.Order)
                        except (ValueError, TypeError, AttributeError):
                            pass
                    else:
                        has_subquery_where = True

        for eq_expr in where_node.find_all(exp.EQ):
            for sub in eq_expr.find_all(exp.Select):
                if sub.find(exp.Where):
                    has_subquery_where = True
                sub_lim = sub.find(exp.Limit)
                if sub_lim:
                    col_bounds_target = _is_column_unique_or_pk(eq_expr.this, effective_target_table, schema)
                    if col_bounds_target:
                        try:
                            lim_val = sub_lim.expression.this if hasattr(sub_lim.expression, "this") else str(sub_lim.expression)
                            in_subquery_limit_count = int(lim_val)
                            bounding_order_node = sub.find(exp.Order)
                        except (ValueError, TypeError, AttributeError):
                            pass
                    else:
                        has_subquery_where = True

    # Check bounding in CTEs (ONLY if the CTE is referenced in the mutation body)
    has_cte_where = False
    if with_node:
        for cte in with_node.expressions:
            cte_name = cte.alias_or_name
            if not cte_name or cte_name.lower() not in mutation_body_tables:
                # UNRELATED CTE: completely skip!
                continue

            cte_lim = cte.find(exp.Limit)
            if cte_lim:
                try:
                    lim_val = cte_lim.expression.this if hasattr(cte_lim.expression, "this") else str(cte_lim.expression)
                    cte_val = int(lim_val)
                except (ValueError, TypeError, AttributeError):
                    cte_val = None

                if cte_val is not None:
                    # Case 1: Target table is directly the CTE alias (e.g. DELETE FROM cte in T-SQL)
                    if target_table and target_table.lower() == cte_name.lower():
                        cte_limit_count = cte_val
                        bounding_order_node = cte.find(exp.Order)
                    # Case 2: Target table WHERE filters on id IN (SELECT id FROM to_delete)
                    elif where_node:
                        for in_node in where_node.find_all(exp.In):
                            sub = in_node.args.get("query")
                            if sub and sub.find(exp.Table) and sub.find(exp.Table).name.lower() == cte_name.lower():
                                if _is_column_unique_or_pk(in_node.this, effective_target_table, schema):
                                    cte_limit_count = cte_val
                                    bounding_order_node = cte.find(exp.Order)
                                else:
                                    has_cte_where = True
            else:
                if cte.find(exp.Where):
                    has_cte_where = True

    # Determine final effective limit count
    effective_limit = None
    if is_dialect_valid and direct_limit_count is not None:
        effective_limit = direct_limit_count
    elif in_subquery_limit_count is not None:
        effective_limit = in_subquery_limit_count
    elif cte_limit_count is not None:
        effective_limit = cte_limit_count

    is_bounded = (effective_limit is not None and is_dialect_valid)
    is_ordered = bool(bounding_order_node is not None)
    is_deterministic = False
    determinism_reason = None

    if is_bounded:
        has_tiebreaker, tb_col = _has_unique_order_tiebreaker(bounding_order_node, effective_target_table, schema)
        if not is_ordered:
            is_deterministic = False
            determinism_reason = "Nondeterministic: No ORDER BY clause (database selects arbitrary records)"
        elif has_tiebreaker:
            is_deterministic = True
            determinism_reason = f"Deterministic: ORDER BY includes verified unique tie-breaker ('{tb_col}')"
        else:
            is_deterministic = False
            ord_cols = [c.name for c in bounding_order_node.find_all(exp.Column)]
            cols_str = ", ".join(f"'{c}'" for c in ord_cols) if ord_cols else "specified columns"
            determinism_reason = f"Partially ordered: ORDER BY ({cols_str}) lacks a verified unique tie-breaker (tied rows may be selected unpredictably)"

    # Check if there are filter conditions beyond pure key-based limit
    has_filter = False
    if has_subquery_where or has_cte_where:
        has_filter = True
    elif where_node:
        filter_nodes = list(where_node.find_all((exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)))
        if filter_nodes:
            has_filter = True

    # Determine scope type
    if not is_dialect_valid:
        scope_type = "INVALID_DIALECT_LIMIT"
        is_unrestricted = False
        is_deterministic = False
        determinism_reason = dialect_error
    elif is_bounded and has_filter:
        scope_type = "BOUNDED_FILTERED"
        is_unrestricted = False
    elif is_bounded:
        scope_type = "BOUNDED"
        is_unrestricted = False
    elif where_node is not None:
        scope_type = "FILTERED"
        is_unrestricted = False
        is_deterministic = True
        determinism_reason = "Deterministic predicate: All rows matching WHERE criteria are modified"
    else:
        scope_type = "FULL_TABLE"
        is_unrestricted = True
        is_deterministic = True
        determinism_reason = "Unrestricted: All rows in the table are targeted"

    return MutationScope(
        operation=op,
        target_table=effective_target_table,
        scope_type=scope_type,
        is_bounded=is_bounded,
        limit_count=effective_limit if is_bounded else None,
        is_deterministic=is_deterministic,
        is_ordered=is_ordered,
        determinism_reason=determinism_reason,
        selection_criteria=selection_criteria,
        is_unrestricted=is_unrestricted,
        is_dialect_valid=is_dialect_valid,
        dialect_error=dialect_error,
    )


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
    matched_table_objs: list[TableSchema] = []

    for tbl in stmt.find_all(exp.Table):
        if not tbl.name:
            continue
        t_name = tbl.name.lower()
        if t_name in cte_names:
            continue

        if is_system_metadata_table_node(tbl):
            tables_ref.add(tbl.name)
            system_metadata_tables.add(t_name)
            continue

        schema_qual = tbl.db if tbl.db else None
        table_obj = schema.get_table(tbl.name, schema_name=schema_qual)
        if table_obj:
            tables_ref.add(table_obj.full_name)
            matched_table_objs.append(table_obj)
        else:
            ref_name = f"{tbl.db}.{tbl.name}" if tbl.db else tbl.name
            unknown_tables.append(ref_name)

    unknown_tables = sorted(list(set(unknown_tables)))

    # 3. Extract columns referenced
    cols_ref: set[str] = set()
    for col_expr in stmt.find_all(exp.Column):
        if col_expr.name and col_expr.name != "*":
            cols_ref.add(col_expr.name.lower())

    # Check unknown columns against application tables only
    unknown_cols = []
    if not unknown_tables and matched_table_objs:
        ref_table_columns = set()
        for table_obj in matched_table_objs:
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
    allow_mutations: bool = False,
    expected_operation: Optional[str] = None,
) -> ValidationResult:
    """Validate that the generated SQL query is a valid statement matching safety and dialect rules.

    Args:
        sql: The SQL query string to validate.
        dialect: The target database dialect.
        schema: Optional discovered DatabaseSchema to verify table and column references.
        allow_mutations: If False, enforces read-only SELECT/UNION statements. If True, permits mutations and DDL.
        expected_operation: Optional operation type to enforce (e.g. 'CREATE TABLE', 'INSERT', 'SELECT').

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

    # Detect statement operation type
    detected_op = "UNKNOWN"
    if isinstance(stmt, (exp.Select, exp.Union)):
        detected_op = "SELECT"
    elif isinstance(stmt, exp.Insert):
        detected_op = "INSERT"
    elif isinstance(stmt, exp.Update):
        detected_op = "UPDATE"
    elif isinstance(stmt, exp.Delete):
        detected_op = "DELETE"
    elif isinstance(stmt, exp.Create):
        if stmt.find(exp.Index) or "INDEX" in str(stmt).upper():
            detected_op = "CREATE INDEX"
        else:
            detected_op = "CREATE TABLE"
    elif isinstance(stmt, exp.Alter):
        detected_op = "ALTER TABLE"
    elif isinstance(stmt, exp.Drop):
        detected_op = "DROP TABLE"
    else:
        detected_op = type(stmt).__name__.upper()

    if expected_operation and expected_operation.upper() != "UNKNOWN":
        if detected_op != expected_operation.upper():
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=(detected_op == "SELECT"),
                operation=detected_op,
                status_message=f"Rejected: Statement operation mismatch (expected {expected_operation.upper()}, found {detected_op})",
            )

    is_readonly_stmt = detected_op == "SELECT"

    # If mutations/DDL are not permitted, enforce read-only SELECT
    if not allow_mutations and (expected_operation is None or expected_operation.upper() == "SELECT"):
        if not is_readonly_stmt:
            op_name = type(stmt).__name__
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=False,
                operation=detected_op,
                status_message=f"Rejected: Only read-only SELECT queries are permitted (found {op_name})",
            )

        # Check for SELECT ... INTO clause creating tables
        if stmt.find(exp.Into):
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=False,
                operation=detected_op,
                status_message="Rejected: Table creation/modification clause ('SELECT INTO') is not permitted in read-only queries",
            )

        # Check for row/table locking clauses (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE)
        if stmt.find(exp.Lock):
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=False,
                operation=detected_op,
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
                        operation=detected_op,
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
                    operation=detected_op,
                    status_message=f"Rejected: Prohibited operation detected in query: {op_name}",
                )
    else:
        # If mutations are allowed:
        if detected_op in ("UPDATE", "DELETE"):
            scope_info = analyze_mutation_scope(stmt, dialect_str=sqlglot_dialect, schema=schema)
            if not scope_info.is_dialect_valid:
                return ValidationResult(
                    is_valid=False,
                    is_parsed=True,
                    is_readonly=False,
                    operation=detected_op,
                    status_message=f"Rejected: {scope_info.dialect_error}",
                    mutation_scope=scope_info.scope_type,
                    mutation_limit=scope_info.limit_count,
                    is_bounded=scope_info.is_bounded,
                    is_deterministic=scope_info.is_deterministic,
                    determinism_reason=scope_info.determinism_reason,
                    is_unrestricted=scope_info.is_unrestricted,
                    selection_criteria=scope_info.selection_criteria,
                    target_table=scope_info.target_table,
                    is_ordered=scope_info.is_ordered,
                )

    # Check for direct or aliased sensitive field projections on any statement projecting columns
    if stmt.find(exp.Select) or stmt.find(exp.Returning):
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
                is_readonly=is_readonly_stmt,
                operation=detected_op,
                status_message=f"Rejected: Query projects sensitive credential/authentication field(s): {sens_str}",
            )

        # Check for unconstrained wildcard projections on tables containing sensitive fields
        has_star, sens_tables = has_sensitive_wildcard(stmt, schema=schema)
        if has_star and sens_tables and sens_tables != ["unknown_tables"]:
            t_str = ", ".join(sens_tables)
            return ValidationResult(
                is_valid=False,
                is_parsed=True,
                is_readonly=is_readonly_stmt,
                operation=detected_op,
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

    # For CREATE TABLE, the table being created is naturally not yet in the schema
    mutation_scope = "READ_ONLY"
    mutation_limit = None
    is_bounded = False
    is_deterministic = False
    determinism_reason = None
    is_unrestricted = False
    selection_criteria = None
    target_table = None
    is_ordered = False
    status_msg = "Passed"

    if detected_op in ("UPDATE", "DELETE"):
        scope_info = analyze_mutation_scope(stmt, dialect_str=sqlglot_dialect, schema=schema)
        mutation_scope = scope_info.scope_type
        mutation_limit = scope_info.limit_count
        is_bounded = scope_info.is_bounded
        is_deterministic = scope_info.is_deterministic
        determinism_reason = scope_info.determinism_reason
        is_unrestricted = scope_info.is_unrestricted
        selection_criteria = scope_info.selection_criteria
        target_table = scope_info.target_table
        is_ordered = scope_info.is_ordered
        if is_unrestricted:
            status_msg = f"Passed: Full-table {detected_op} (unrestricted mutation - high impact)"
        elif is_bounded and scope_info.scope_type == "BOUNDED_FILTERED":
            status_msg = f"Passed: Bounded & filtered {detected_op} (limit: {mutation_limit} rows)"
        elif is_bounded:
            status_msg = f"Passed: Bounded {detected_op} (limit: {mutation_limit} rows)"
        else:
            status_msg = f"Passed: Targeted {detected_op} with WHERE clause"
    elif detected_op == "CREATE TABLE":
        status_msg = "Passed"

    return ValidationResult(
        is_valid=True,
        is_parsed=True,
        is_readonly=is_readonly_stmt,
        operation=detected_op,
        status_message=status_msg,
        tables_referenced=tables_ref,
        columns_referenced=cols_ref,
        unknown_tables=unknown_tables,
        unknown_columns=unknown_cols,
        is_schema_verified=is_schema_ver,
        schema_status=schema_msg,
        mutation_scope=mutation_scope,
        mutation_limit=mutation_limit,
        is_bounded=is_bounded,
        is_deterministic=is_deterministic,
        determinism_reason=determinism_reason,
        is_unrestricted=is_unrestricted,
        selection_criteria=selection_criteria,
        target_table=target_table,
        is_ordered=is_ordered,
    )
