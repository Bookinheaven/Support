"""SQLite database connectivity and schema introspection implementation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from support.application.errors import (
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    ObjectNotFoundError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import SQLiteConnection
from support.domain.schema import (
    ColumnMetadata,
    ConstraintMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    TableMetadata,
    TableType,
    unquote_identifier,
)

DEFAULT_SQLITE_TIMEOUT_SECONDS = 10.0
SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
SQLITE_HEADER_SIZE = 100
VALID_PAGE_SIZES = {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}


def _quote_identifier(name: str) -> str:
    """Safely quote SQLite identifier by wrapping in double quotes and escaping embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _validate_sqlite_file(path: Path, connection_path: str) -> None:
    """Validate that the file exists, is readable, and contains a valid SQLite header."""
    if not path.exists():
        raise DatabaseNotFoundError(
            f"SQLite database file not found: '{connection_path}'."
        )

    if path.is_dir():
        raise GeneralConnectionError(
            f"Provided path is a directory, not an SQLite database file: '{connection_path}'."
        )

    if not os.access(path, os.R_OK):
        raise GeneralConnectionError(
            f"Access denied or unreadable SQLite database file: '{connection_path}'."
        )

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise GeneralConnectionError(
            f"Unable to read file metadata for SQLite database: '{connection_path}'."
        ) from exc

    if file_size == 0:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is an empty file (0 bytes)."
        )

    if file_size < SQLITE_HEADER_SIZE:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is smaller than the 100-byte SQLite header."
        )

    try:
        with path.open("rb") as f:
            header = f.read(SQLITE_HEADER_SIZE)
    except OSError as exc:
        raise GeneralConnectionError(
            f"Access denied or unreadable SQLite database file: '{connection_path}'."
        ) from exc

    if len(header) < SQLITE_HEADER_SIZE:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' has an incomplete header."
        )

    if header[:16] != SQLITE_HEADER_MAGIC:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' is missing SQLite magic header."
        )

    raw_page_size = int.from_bytes(header[16:18], byteorder="big")
    page_size = 65536 if raw_page_size == 1 else raw_page_size
    if page_size not in VALID_PAGE_SIZES:
        raise GeneralConnectionError(
            f"Invalid or malformed SQLite database: '{connection_path}' has invalid page size ({page_size})."
        )


def _extract_sqlite_checks(table_sql: str | None) -> list[ConstraintMetadata]:
    """Extract declared CHECK constraints from SQLite table DDL using paren balancing."""
    if not table_sql:
        return []
    checks: list[ConstraintMetadata] = []
    pattern = re.compile(r'(?:CONSTRAINT\s+([a-zA-Z0-9_\"]+)\s+)?CHECK\s*\(', re.IGNORECASE)
    for m in pattern.finditer(table_sql):
        raw_name = m.group(1)
        c_name = raw_name.strip('"') if raw_name else None
        start_idx = m.end() - 1
        depth = 0
        clause_chars: list[str] = []
        for i in range(start_idx, len(table_sql)):
            ch = table_sql[i]
            if ch == "(":
                depth += 1
                if depth > 1:
                    clause_chars.append(ch)
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
                else:
                    clause_chars.append(ch)
            else:
                clause_chars.append(ch)
        if depth == 0:
            clause = "".join(clause_chars).strip()
            checks.append(
                ConstraintMetadata(
                    name=c_name,
                    columns=(),
                    constraint_type="CHECK",
                    check_clause=clause,
                )
            )
    return checks


def _introspect_single_sqlite_table(cur: sqlite3.Cursor, table_name: str, obj_type: str) -> TableMetadata:
    """Introspect columns, primary keys, foreign keys, constraints, and indexes for a single SQLite table or view."""
    quoted_name = _quote_identifier(table_name)
    tbl_type = TableType.VIEW if obj_type == "view" else TableType.TABLE

    # Fetch table DDL from sqlite_schema / sqlite_master
    table_sql: str | None = None
    try:
        cur.execute("SELECT sql FROM sqlite_schema WHERE type IN ('table', 'view') AND name = ?", (table_name,))
        r = cur.fetchone()
        if r:
            table_sql = r[0]
    except sqlite3.OperationalError:
        try:
            cur.execute("SELECT sql FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (table_name,))
            r = cur.fetchone()
            if r:
                table_sql = r[0]
        except sqlite3.Error:
            pass

    # Columns
    try:
        cur.execute(f"PRAGMA table_xinfo({quoted_name});")
        col_rows = cur.fetchall()
    except sqlite3.Error:
        cur.execute(f"PRAGMA table_info({quoted_name});")
        col_rows = [(*r, 0) for r in cur.fetchall()]  # append hidden=0

    columns: list[ColumnMetadata] = []
    pk_cols_with_pos: list[tuple[int, str]] = []
    for row in col_rows:
        cid = row[0]
        name = row[1]
        col_type = row[2]
        notnull = row[3]
        dflt_val = row[4]
        pk = row[5]
        hidden = row[6] if len(row) > 6 else 0
        # hidden == 1 is an internal hidden column in virtual tables; hidden == 2 (stored) or 3 (virtual) are generated columns
        if hidden == 1:
            continue
        columns.append(
            ColumnMetadata(
                name=name,
                data_type=col_type or "BLOB",
                nullable=(notnull == 0),
                default=dflt_val,
                ordinal_position=cid + 1,
            )
        )
        if pk > 0:
            pk_cols_with_pos.append((pk, name))

    pk_cols_with_pos.sort(key=lambda x: x[0])
    pk_constraint = None
    if pk_cols_with_pos:
        pk_constraint = ConstraintMetadata(
            name=f"pk_{table_name}",
            columns=tuple(col for _, col in pk_cols_with_pos),
            constraint_type="PRIMARY KEY",
        )

    # Check Constraints from table DDL
    check_constraints = _extract_sqlite_checks(table_sql)

    # Foreign Keys
    fks: list[ForeignKeyMetadata] = []
    if tbl_type == TableType.TABLE:
        try:
            cur.execute(f"PRAGMA foreign_key_list({quoted_name});")
            fk_rows = cur.fetchall()
            grouped_fks: dict[int, list[Any]] = {}
            for r in fk_rows:
                grouped_fks.setdefault(r[0], []).append(r)
            for _, r_list in grouped_fks.items():
                r_list.sort(key=lambda x: x[1])  # sort by seq
                tgt_table = r_list[0][2]
                src_cols = tuple(r[3] for r in r_list)
                tgt_cols = tuple(r[4] for r in r_list)
                on_upd = r_list[0][5]
                on_del = r_list[0][6]
                fks.append(
                    ForeignKeyMetadata(
                        name=None,
                        source_table=table_name,
                        source_columns=src_cols,
                        target_table=tgt_table,
                        target_columns=tgt_cols,
                        on_delete=on_del if on_del != "NO ACTION" else None,
                        on_update=on_upd if on_upd != "NO ACTION" else None,
                    )
                )
        except sqlite3.Error:
            pass

    # Indexes and Unique Constraints
    indexes: list[IndexMetadata] = []
    unique_constraints: list[ConstraintMetadata] = []
    if tbl_type == TableType.TABLE:
        try:
            cur.execute(f"PRAGMA index_list({quoted_name});")
            idx_rows = cur.fetchall()
            for i_row in idx_rows:
                # index_list: (seq, name, unique, origin, partial)
                idx_name = i_row[1]
                is_unique = bool(i_row[2])
                origin = i_row[3]
                is_partial = bool(i_row[4]) if len(i_row) > 4 else False
                quoted_idx = _quote_identifier(idx_name)

                # Get index DDL for expression or partial filter
                idx_sql: str | None = None
                try:
                    cur.execute("SELECT sql FROM sqlite_schema WHERE type = 'index' AND name = ?", (idx_name,))
                    sq_r = cur.fetchone()
                    if sq_r:
                        idx_sql = sq_r[0]
                except sqlite3.OperationalError:
                    try:
                        cur.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (idx_name,))
                        sq_r = cur.fetchone()
                        if sq_r:
                            idx_sql = sq_r[0]
                    except sqlite3.Error:
                        pass

                filter_cond = None
                if idx_sql and ("WHERE" in idx_sql.upper() or is_partial):
                    m_where = re.search(r"\s+WHERE\s+(.+)$", idx_sql, re.IGNORECASE)
                    if m_where:
                        filter_cond = m_where.group(1).strip()

                col_names: list[str] = []
                col_orders: list[str] = []
                try:
                    cur.execute(f"PRAGMA index_xinfo({quoted_idx});")
                    xinfo_rows = cur.fetchall()
                    for xr in xinfo_rows:
                        is_key = xr[5] if len(xr) > 5 else 1
                        if not is_key:
                            continue
                        c_name = xr[2]
                        is_desc = bool(xr[3]) if len(xr) > 3 else False
                        ord_dir = "DESC" if is_desc else "ASC"
                        if c_name is None and idx_sql:
                            core = idx_sql
                            if filter_cond and "WHERE" in core.upper():
                                core = core[:core.upper().rfind("WHERE")]
                            p_match = re.search(r"\((.+)\)", core)
                            if p_match:
                                expr_raw = p_match.group(1).strip()
                                expr_upper = expr_raw.upper()
                                if expr_upper.endswith(" ASC"):
                                    expr_raw = expr_raw[:-4].strip()
                                    ord_dir = "ASC"
                                elif expr_upper.endswith(" DESC"):
                                    expr_raw = expr_raw[:-5].strip()
                                    ord_dir = "DESC"
                                c_name = expr_raw
                        col_names.append(c_name or "(expression)")
                        col_orders.append(ord_dir)
                except sqlite3.Error:
                    cur.execute(f"PRAGMA index_info({quoted_idx});")
                    info_rows = cur.fetchall()
                    info_rows.sort(key=lambda x: x[0])
                    for ir in info_rows:
                        col_names.append(ir[2] or "(expression)")
                        col_orders.append("ASC")

                is_pk = (origin == "pk")
                indexes.append(
                    IndexMetadata(
                        name=idx_name,
                        columns=tuple(col_names),
                        unique=is_unique,
                        is_primary=is_pk,
                        method="BTREE",
                        column_orders=tuple(col_orders),
                        filter_condition=filter_cond,
                    )
                )
                if is_unique and not is_pk:
                    unique_constraints.append(
                        ConstraintMetadata(
                            name=idx_name,
                            columns=tuple(col_names),
                            constraint_type="UNIQUE",
                        )
                    )
        except sqlite3.Error:
            pass

    return TableMetadata(
        name=table_name,
        table_type=tbl_type,
        schema_name=None,
        columns=tuple(columns),
        primary_key=pk_constraint,
        unique_constraints=tuple(unique_constraints),
        foreign_keys=tuple(fks),
        check_constraints=tuple(check_constraints),
        indexes=tuple(indexes),
    )


class SQLiteConnector:
    """Infrastructure connector for establishing, testing, and introspecting SQLite databases."""

    def __init__(self, timeout_seconds: float = DEFAULT_SQLITE_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def _open_connection(self, connection: SQLiteConnection) -> sqlite3.Connection:
        """Open a read-only SQLite connection adhering to safety rules."""
        if connection.database_path == ":memory:":
            return sqlite3.connect(":memory:", timeout=self._timeout_seconds)

        path = Path(connection.database_path).expanduser()
        if not path.is_absolute():
            path = path.resolve()

        _validate_sqlite_file(path, connection.database_path)
        uri = f"{path.as_uri()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self._timeout_seconds)
            conn.execute("PRAGMA schema_version;")
            return conn
        except sqlite3.DatabaseError as exc:
            err_msg = str(exc).lower()
            if "not a database" in err_msg or "malformed" in err_msg or "corrupt" in err_msg:
                raise GeneralConnectionError(
                    f"Invalid or malformed SQLite database: '{connection.database_path}'."
                ) from exc
            raise GeneralConnectionError(f"SQLite connection failed: {exc}") from exc
        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            if "unable to open database file" in err_msg:
                if not path.exists():
                    raise DatabaseNotFoundError(
                        f"SQLite database file not found: '{connection.database_path}'."
                    ) from exc
                raise GeneralConnectionError(
                    f"Unable to open SQLite database file: '{connection.database_path}'."
                ) from exc
            if "locked" in err_msg or "busy" in err_msg:
                raise ConnectionTimeoutError(
                    f"SQLite database is locked or busy: '{connection.database_path}'."
                ) from exc
            if "permission denied" in err_msg or "access denied" in err_msg:
                raise GeneralConnectionError(
                    f"Access denied to SQLite database file: '{connection.database_path}'."
                ) from exc
            raise GeneralConnectionError(f"SQLite operational error: {exc}") from exc
        except sqlite3.Error as exc:
            raise GeneralConnectionError(f"SQLite error: {exc}") from exc

    def test_connection(
        self,
        connection: SQLiteConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Verify database connectivity in read-only mode."""
        conn = None
        try:
            conn = self._open_connection(connection)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            row = cur.fetchone()
            if row is None or row[0] != 1:
                raise GeneralConnectionError(
                    f"Database verification query failed: expected 1, got {row}"
                )

            db_identifier = ":memory:" if connection.database_path == ":memory:" else (Path(connection.database_path).name or connection.database_path)
            return ConnectionTestResult(
                success=True,
                database_type="sqlite",
                database=db_identifier,
                server_version=sqlite3.sqlite_version,
                message="Connection successful.",
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def introspect_schema(
        self,
        connection: SQLiteConnection,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect full SQLite database schema."""
        conn = self._open_connection(connection)
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT name, type FROM sqlite_schema "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' AND name != 'sqlite_sequence' "
                    "ORDER BY name;"
                )
            except sqlite3.OperationalError:
                cur.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' AND name != 'sqlite_sequence' "
                    "ORDER BY name;"
                )
            objects = cur.fetchall()
            tables: list[TableMetadata] = []
            all_fks: list[ForeignKeyMetadata] = []

            for name, obj_type in objects:
                tbl_meta = _introspect_single_sqlite_table(cur, name, obj_type)
                tables.append(tbl_meta)
                all_fks.extend(tbl_meta.foreign_keys)

            db_name = ":memory:" if connection.database_path == ":memory:" else (Path(connection.database_path).name or connection.database_path)
            return DatabaseSchema(
                database_name=db_name,
                engine="SQLite",
                schemas=(),
                tables=tuple(tables),
                relationships=tuple(all_fks),
            )
        finally:
            conn.close()

    def introspect_table(
        self,
        connection: SQLiteConnection,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single SQLite table or view."""
        clean_name = unquote_identifier(table_name)
        conn = self._open_connection(connection)
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT name, type FROM sqlite_schema "
                    "WHERE type IN ('table', 'view') AND (name = ? OR lower(name) = lower(?)) AND name NOT LIKE 'sqlite_%';",
                    (clean_name, clean_name),
                )
            except sqlite3.OperationalError:
                cur.execute(
                    "SELECT name, type FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND (name = ? OR lower(name) = lower(?)) AND name NOT LIKE 'sqlite_%';",
                    (clean_name, clean_name),
                )
            row = cur.fetchone()
            if not row:
                raise ObjectNotFoundError(f"Object '{table_name}' not found in SQLite database.")
            actual_name, obj_type = row
            return _introspect_single_sqlite_table(cur, actual_name, obj_type)
        finally:
            conn.close()
