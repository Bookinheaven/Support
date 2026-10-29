"""SQL DDL parsing and dialect detection using sqlglot."""

import re
from pathlib import Path
from typing import Optional, Any
import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import ParseError

from support.core.dialect import Dialect
from support.core.exceptions import SQLParseError
from support.schema.models import (
    DatabaseSchema,
    TableSchema,
    ColumnSchema,
    ForeignKeySchema,
    UniqueConstraintSchema,
)
from support.schema.base import DiscoveryWarning


def detect_dialect_from_sql(sql_content: str) -> Dialect:
    """Analyze SQL keywords to detect the most probable database dialect.

    Returns Dialect.UNKNOWN if no strong or unambiguous evidence is present.
    """
    content_upper = sql_content.upper()
    postgres_score = 0
    mysql_score = 0
    sqlite_score = 0
    mssql_score = 0

    # PostgreSQL indicators
    if re.search(r"\b(SERIAL|BIGSERIAL|SMALLSERIAL)\b", content_upper):
        postgres_score += 4
    if re.search(r"\b(JSONB|BYTEA|CITEXT|TIMESTAMPTZ|INET|UUID)\b", content_upper):
        postgres_score += 3
    if re.search(r"\bBOOLEAN\s+DEFAULT\s+(TRUE|FALSE)\b", content_upper):
        postgres_score += 2
    if re.search(r"\bCREATE\s+EXTENSION\b", content_upper):
        postgres_score += 4

    # MySQL indicators
    if re.search(r"\bAUTO_INCREMENT\b", content_upper):
        mysql_score += 4
    if re.search(r"\bENGINE\s*=\s*(INNODB|MYISAM|MEMORY)\b", content_upper):
        mysql_score += 5
    if re.search(r"\bDEFAULT\s+CHARSET\b", content_upper):
        mysql_score += 4
    if re.search(r"\b(MEDIUMINT|LONGTEXT|MEDIUMTEXT|TINYINT\s*\(\s*1\s*\))\b", content_upper):
        mysql_score += 2

    # SQLite indicators
    if re.search(r"\bAUTOINCREMENT\b", content_upper) and not re.search(r"\bAUTO_INCREMENT\b", content_upper):
        sqlite_score += 4
    if re.search(r"\bWITHOUT\s+ROWID\b", content_upper):
        sqlite_score += 5

    # MSSQL indicators
    if re.search(r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", content_upper):
        mssql_score += 4
    if re.search(r"\b(NVARCHAR\(MAX\)|VARCHAR\(MAX\)|VARBINARY\(MAX\)|DATETIME2|DATETIMEOFFSET)\b", content_upper):
        mssql_score += 3
    if re.search(r"\bGO\s*$", sql_content, re.MULTILINE):
        mssql_score += 3

    scores = [
        (postgres_score, Dialect.POSTGRES),
        (mysql_score, Dialect.MYSQL),
        (sqlite_score, Dialect.SQLITE),
        (mssql_score, Dialect.MSSQL),
    ]
    scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_dialect = scores[0]
    second_score = scores[1][0]

    if best_score >= 3 and best_score > second_score:
        return best_dialect
    return Dialect.UNKNOWN


class SQLDDLParser:
    """Parses raw SQL DDL statements into normalized schema objects."""

    def __init__(self, dialect: Dialect = Dialect.UNKNOWN) -> None:
        self.dialect = dialect

    def parse_sql(
        self,
        sql_content: str,
        source_origin: Optional[str] = None,
    ) -> tuple[DatabaseSchema, list[DiscoveryWarning]]:
        """Parse raw SQL content into a normalized DatabaseSchema.

        Returns a tuple of (schema, warnings).
        Raises SQLParseError if parsing fails completely.
        """
        warnings: list[DiscoveryWarning] = []

        # Determine effective dialect
        effective_dialect = self.dialect
        if effective_dialect == Dialect.UNKNOWN:
            detected = detect_dialect_from_sql(sql_content)
            if detected != Dialect.UNKNOWN:
                effective_dialect = detected

        read_dialect = effective_dialect.sqlglot_read_dialect

        # Attempt to parse statements
        statements: list[exp.Expression] = []
        parse_err: Optional[Exception] = None

        try:
            statements = [s for s in sqlglot.parse(sql_content, read=read_dialect) if s is not None]
        except ParseError as e:
            parse_err = e

        # If failed and dialect was UNKNOWN, attempt fallback dialects
        if not statements and (effective_dialect == Dialect.UNKNOWN or parse_err is not None):
            for candidate in ("postgres", "mysql", "sqlite", "tsql"):
                try:
                    fallback_stmts = [s for s in sqlglot.parse(sql_content, read=candidate) if s is not None]
                    if fallback_stmts:
                        statements = fallback_stmts
                        effective_dialect = Dialect.from_string(candidate)
                        parse_err = None
                        break
                except Exception:
                    continue

        if parse_err is not None and not statements:
            raise SQLParseError(source_origin, str(parse_err))

        schema = DatabaseSchema(dialect=effective_dialect)
        if source_origin:
            schema.source_origins.append(source_origin)

        # Process statements
        for stmt in statements:
            if isinstance(stmt, exp.Create):
                self._process_create_statement(stmt, schema, source_origin, warnings)
            elif isinstance(stmt, exp.Alter):
                self._process_alter_statement(stmt, schema, source_origin, warnings)

        return schema, warnings

    def _extract_table_identifiers(self, table_exp: Any) -> tuple[str, Optional[str]]:
        """Extract table name and schema from an AST Table or Schema node."""
        if hasattr(table_exp, "this") and isinstance(table_exp.this, exp.Table):
            table_name = table_exp.this.name
            schema_name = table_exp.this.db or None
            return table_name, schema_name
        if isinstance(table_exp, exp.Table):
            return table_exp.name, table_exp.db or None
        if hasattr(table_exp, "name"):
            return table_exp.name, getattr(table_exp, "db", None) or None
        return str(table_exp), None

    def _process_create_statement(
        self,
        stmt: exp.Create,
        schema: DatabaseSchema,
        source_origin: Optional[str],
        warnings: list[DiscoveryWarning],
    ) -> None:
        """Process CREATE TABLE statements."""
        # Only process CREATE TABLE
        kind = getattr(stmt, "kind", None)
        if kind and str(kind).upper() != "TABLE":
            return

        target_schema = stmt.this
        if not target_schema:
            return

        table_name, schema_name = self._extract_table_identifiers(target_schema)
        if not table_name:
            return

        table = schema.get_table(table_name, schema_name)
        if table is None:
            table = TableSchema(
                name=table_name,
                schema_name=schema_name,
                source_origin=source_origin,
                raw_ddl=stmt.sql(),
            )
            if source_origin:
                table.source_origins.append(source_origin)
            schema.add_table(table)

        # Iterate over expressions inside table definition
        expressions = getattr(target_schema, "expressions", [])
        for expr in expressions:
            if isinstance(expr, exp.ColumnDef):
                self._process_column_def(expr, table, source_origin)
            elif isinstance(expr, exp.Constraint):
                self._process_table_constraint(expr, table, warnings)
            elif isinstance(expr, exp.PrimaryKey):
                self._process_table_primary_key(expr, table)
            elif isinstance(expr, exp.ForeignKey):
                self._process_table_foreign_key(expr, None, table)
            elif isinstance(expr, exp.UniqueColumnConstraint):
                self._process_table_unique(expr, None, table)

    def _process_column_def(
        self,
        col_def: exp.ColumnDef,
        table: TableSchema,
        source_origin: Optional[str],
    ) -> None:
        """Parse individual column definition and column constraints."""
        col_name = col_def.this.name if hasattr(col_def.this, "name") else str(col_def.this)
        data_type = col_def.kind.sql() if col_def.kind else "UNKNOWN"

        col = ColumnSchema(
            name=col_name,
            data_type=data_type,
            source_origin=source_origin,
        )

        for constraint in getattr(col_def, "constraints", []):
            kind = getattr(constraint, "kind", None)
            if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                col.is_primary_key = True
                if col_name not in table.primary_key:
                    table.primary_key.append(col_name)
            elif isinstance(kind, exp.NotNullColumnConstraint):
                col.nullable = False
            elif isinstance(kind, exp.UniqueColumnConstraint):
                col.is_unique = True
            elif isinstance(kind, exp.DefaultColumnConstraint):
                if hasattr(kind, "this") and kind.this:
                    col.default_value = kind.this.sql()
            elif isinstance(kind, exp.Reference):
                # Inline foreign key
                self._process_inline_reference(col_name, kind, table)

        table.add_or_update_column(col)

    def _process_inline_reference(
        self,
        col_name: str,
        ref: exp.Reference,
        table: TableSchema,
    ) -> None:
        """Extract foreign key from inline column reference."""
        target_schema_node = ref.this
        if not target_schema_node:
            return

        ref_table, ref_schema = self._extract_table_identifiers(target_schema_node)
        ref_cols: list[str] = []

        if hasattr(target_schema_node, "expressions"):
            for e in target_schema_node.expressions:
                if hasattr(e, "name"):
                    ref_cols.append(e.name)

        on_delete, on_update = self._extract_fk_options(ref)

        fk = ForeignKeySchema(
            name=None,
            constrained_columns=[col_name],
            referred_table=ref_table,
            referred_columns=ref_cols,
            referred_schema=ref_schema,
            on_delete=on_delete,
            on_update=on_update,
        )
        table.foreign_keys.append(fk)

    def _process_table_constraint(
        self,
        constraint: exp.Constraint,
        table: TableSchema,
        warnings: list[DiscoveryWarning],
    ) -> None:
        """Process named or wrapped table constraint."""
        constraint_name = constraint.this.name if hasattr(constraint.this, "name") else None
        for inner in getattr(constraint, "expressions", []):
            if isinstance(inner, exp.PrimaryKey):
                self._process_table_primary_key(inner, table)
            elif isinstance(inner, exp.ForeignKey):
                self._process_table_foreign_key(inner, constraint_name, table)
            elif isinstance(inner, exp.UniqueColumnConstraint):
                self._process_table_unique(inner, constraint_name, table)

    def _process_table_primary_key(self, pk_node: exp.PrimaryKey, table: TableSchema) -> None:
        """Extract composite or table-level primary key."""
        for expr in getattr(pk_node, "expressions", []):
            col_name = expr.name if hasattr(expr, "name") else str(expr)
            if col_name not in table.primary_key:
                table.primary_key.append(col_name)
            col = table.get_column(col_name)
            if col:
                col.is_primary_key = True

    def _process_table_foreign_key(
        self,
        fk_node: exp.ForeignKey,
        constraint_name: Optional[str],
        table: TableSchema,
    ) -> None:
        """Extract table-level foreign key constraint."""
        source_cols: list[str] = []
        for expr in getattr(fk_node, "expressions", []):
            source_cols.append(expr.name if hasattr(expr, "name") else str(expr))

        ref = fk_node.args.get("reference")
        if not ref:
            return

        target_schema_node = ref.this
        ref_table, ref_schema = self._extract_table_identifiers(target_schema_node)
        ref_cols: list[str] = []

        if hasattr(target_schema_node, "expressions"):
            for e in target_schema_node.expressions:
                if hasattr(e, "name"):
                    ref_cols.append(e.name)

        on_delete, on_update = self._extract_fk_options(ref)

        fk = ForeignKeySchema(
            name=constraint_name,
            constrained_columns=source_cols,
            referred_table=ref_table,
            referred_columns=ref_cols,
            referred_schema=ref_schema,
            on_delete=on_delete,
            on_update=on_update,
        )
        table.foreign_keys.append(fk)

    def _process_table_unique(
        self,
        uq_node: exp.UniqueColumnConstraint,
        constraint_name: Optional[str],
        table: TableSchema,
    ) -> None:
        """Extract unique constraint."""
        cols: list[str] = []
        # In sqlglot, columns can be in uq_node.this.expressions or uq_node.expressions
        inner_schema = getattr(uq_node, "this", None)
        if inner_schema and hasattr(inner_schema, "expressions"):
            for e in inner_schema.expressions:
                cols.append(e.name if hasattr(e, "name") else str(e))
        else:
            for e in getattr(uq_node, "expressions", []):
                cols.append(e.name if hasattr(e, "name") else str(e))

        if cols:
            table.unique_constraints.append(
                UniqueConstraintSchema(name=constraint_name, columns=cols)
            )
            # If single column, mark column as unique
            if len(cols) == 1:
                col = table.get_column(cols[0])
                if col:
                    col.is_unique = True

    def _extract_fk_options(self, ref: exp.Reference) -> tuple[Optional[str], Optional[str]]:
        """Extract ON DELETE and ON UPDATE actions from Reference options."""
        on_delete: Optional[str] = None
        on_update: Optional[str] = None
        options = ref.args.get("options", [])
        for opt in options:
            opt_str = str(opt).upper().strip()
            if "ON DELETE" in opt_str:
                on_delete = opt_str.replace("ON DELETE", "").strip()
            elif "ON UPDATE" in opt_str:
                on_update = opt_str.replace("ON UPDATE", "").strip()
        return on_delete, on_update

    def _process_alter_statement(
        self,
        stmt: exp.Alter,
        schema: DatabaseSchema,
        source_origin: Optional[str],
        warnings: list[DiscoveryWarning],
    ) -> None:
        """Process ALTER TABLE statements (adding constraints or columns)."""
        table_name, schema_name = self._extract_table_identifiers(stmt.this)
        if not table_name:
            return

        table = schema.get_table(table_name, schema_name)
        if not table:
            # Table might be defined in another statement or file; create placeholder
            table = TableSchema(
                name=table_name,
                schema_name=schema_name,
                source_origin=source_origin,
            )
            if source_origin:
                table.source_origins.append(source_origin)
            schema.add_table(table)

        actions = getattr(stmt, "actions", [])
        for action in actions:
            if isinstance(action, exp.ColumnDef):
                self._process_column_def(action, table, source_origin)
            elif isinstance(action, exp.AddConstraint):
                for expr in getattr(action, "expressions", []):
                    if isinstance(expr, exp.Constraint):
                        self._process_table_constraint(expr, table, warnings)
                    elif isinstance(expr, exp.ForeignKey):
                        self._process_table_foreign_key(expr, None, table)
                    elif isinstance(expr, exp.PrimaryKey):
                        self._process_table_primary_key(expr, table)
                    elif isinstance(expr, exp.UniqueColumnConstraint):
                        self._process_table_unique(expr, None, table)
