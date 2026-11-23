"""Microsoft SQL Server connectivity and schema introspection implementation using official mssql-python."""

from __future__ import annotations

import re
from typing import Any

from support.application.errors import (
    AmbiguousObjectNameError,
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    ObjectNotFoundError,
    SSLVerificationError,
    UnsupportedDatabaseDriverError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import SQLServerConnection
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

DEFAULT_SQLSERVER_TIMEOUT_SECONDS: int = 10


class SQLServerConnector:
    """Infrastructure connector for establishing, testing, and introspecting Microsoft SQL Server databases."""

    def __init__(
        self,
        login_timeout: int = DEFAULT_SQLSERVER_TIMEOUT_SECONDS,
        statement_timeout: int = DEFAULT_SQLSERVER_TIMEOUT_SECONDS,
    ) -> None:
        self._login_timeout = login_timeout
        self._statement_timeout = statement_timeout

    def _translate_error(
        self,
        exc: Exception,
        connection: SQLServerConnection,
        password: str | None = None,
    ) -> None:
        """Translate mssql-python exceptions into specific application connection errors."""
        driver_err = (getattr(exc, "driver_error", None) or "").strip()
        ddbc_err = (getattr(exc, "ddbc_error", None) or "").strip()
        sqlstate = getattr(exc, "sqlstate", None) or ""

        driver_err_lower = driver_err.lower()
        ddbc_err_lower = ddbc_err.lower()

        if (
            sqlstate == "28000"
            or "invalid authorization specification" in driver_err_lower
            or "login failed" in ddbc_err_lower
        ):
            raise AuthenticationFailedError(
                f"Authentication failed for user '{connection.username}'.",
                used_saved_password=False,
            ) from exc

        if (
            sqlstate == "3D000"
            or "invalid catalog name" in driver_err_lower
        ):
            raise DatabaseNotFoundError(
                f"Database '{connection.database}' does not exist on server '{connection.host}:{connection.port}'."
            ) from exc

        if (
            "cannot open database" in ddbc_err_lower
            or "could not open database" in ddbc_err_lower
            or ("database" in ddbc_err_lower and "permission" in ddbc_err_lower)
        ):
            raise GeneralConnectionError(
                "The requested database could not be opened. "
                "Verify that it exists and that the login has permission to access it."
            ) from exc

        if (
            "certificate verify failed" in ddbc_err_lower
            or "ssl" in driver_err_lower
            or "ssl" in ddbc_err_lower
            or "certificate" in driver_err_lower
        ):
            raise SSLVerificationError(
                f"TLS/SSL verification failed connecting to '{connection.host}:{connection.port}' "
                f"(encrypt={connection.encrypt}, trust_server_certificate={connection.trust_server_certificate})."
            ) from exc

        if (
            "timeout" in driver_err_lower
            or "timeout" in ddbc_err_lower
            or "login timeout expired" in ddbc_err_lower
        ):
            raise ConnectionTimeoutError(
                f"Connection timed out connecting to '{connection.host}:{connection.port}' after {self._login_timeout} seconds."
            ) from exc

        if (
            sqlstate == "08001"
            or "server not found" in driver_err_lower
            or "server not found" in ddbc_err_lower
            or "connection refused" in ddbc_err_lower
            or "failed to connect" in ddbc_err_lower
            or "unable to connect" in driver_err_lower
            or "unable to establish connection" in driver_err_lower
        ):
            raise HostUnreachableError(
                f"Could not connect to SQL Server at '{connection.host}:{connection.port}'. "
                "Verify host, port, and network reachability."
            ) from exc

        clean_detail = driver_err or ddbc_err or str(exc)
        if password and password in clean_detail:
            clean_detail = clean_detail.replace(password, "******")
        clean_detail = re.sub(r"(?i)pwd\s*=\s*[^;]+", "pwd=******", clean_detail)
        clean_detail = re.sub(r"(?i)password\s*=\s*[^;]+", "password=******", clean_detail)

        raise GeneralConnectionError(
            f"SQL Server connection failed for '{connection.host}:{connection.port}': {clean_detail}"
        ) from exc

    def _open_connection(
        self,
        connection: SQLServerConnection,
        password: str | None = None,
    ) -> Any:
        """Open an mssql-python connection with exact parameters."""
        try:
            import mssql_python

            mssql_python.pooling(enabled=False)
        except ModuleNotFoundError as exc:
            if exc.name in ("mssql_python", "mssql-python"):
                raise UnsupportedDatabaseDriverError(
                    "SQL Server driver 'mssql-python' is not installed. "
                    'Install it using: pip install "support-db[sqlserver]"'
                ) from exc
            raise GeneralConnectionError(
                f"Failed to load SQL Server driver due to missing dependency or library: {exc}"
            ) from exc
        except ImportError as exc:
            raise GeneralConnectionError(
                f"Failed to load SQL Server driver due to import error: {exc}"
            ) from exc
        except Exception as exc:
            raise GeneralConnectionError(
                f"Failed to initialize SQL Server driver (mssql-python): {exc}"
            ) from exc

        connect_kwargs: dict[str, Any] = {
            "Server": f"{connection.host},{connection.port}",
            "Database": connection.database,
            "UID": connection.username,
            "Encrypt": "yes" if connection.encrypt else "no",
            "TrustServerCertificate": "yes" if connection.trust_server_certificate else "no",
            "timeout": self._login_timeout,
            "autocommit": True,
        }
        if password is not None:
            connect_kwargs["PWD"] = password

        try:
            conn = mssql_python.connect(**connect_kwargs)
            conn.timeout = self._statement_timeout
            return conn
        except (
            AuthenticationFailedError,
            DatabaseNotFoundError,
            ConnectionTimeoutError,
            SSLVerificationError,
            HostUnreachableError,
            GeneralConnectionError,
        ):
            raise
        except Exception as exc:
            self._translate_error(exc, connection, password=password)
            raise

    def test_connection(
        self,
        connection: SQLServerConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Establish connection with exact parameters, run SELECT 1, and return result."""
        conn = None
        cursor = None
        try:
            conn = self._open_connection(connection, password=password)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                row = cursor.fetchone()

                if row is None or row[0] != 1:
                    raise GeneralConnectionError(
                        "SQL Server verification query 'SELECT 1' returned unexpected result."
                    )
            except Exception as exc:
                if isinstance(exc, (GeneralConnectionError, ConnectionTimeoutError, AuthenticationFailedError, DatabaseNotFoundError, HostUnreachableError, SSLVerificationError)):
                    raise
                self._translate_error(exc, connection, password=password)

            return ConnectionTestResult(
                success=True,
                database_type="sqlserver",
                database=connection.database,
                host=connection.host,
                port=connection.port,
                username=connection.username,
                server_version=None,
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def introspect_schema(
        self,
        connection: SQLServerConnection,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect full Microsoft SQL Server database schema."""
        conn = self._open_connection(connection, password=password)
        cursor = None
        try:
            cursor = conn.cursor()

            # 1. Schemas
            cursor.execute(
                "SELECT name FROM sys.schemas "
                "WHERE name NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') "
                "  AND principal_id = 1 "
                "ORDER BY name;"
            )
            schemas = tuple(r[0] for r in cursor.fetchall())

            # 2. Tables and views
            cursor.execute(
                "SELECT s.name AS schema_name, o.name AS object_name, o.type_desc AS object_type "
                "FROM sys.objects o "
                "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                "WHERE o.type IN ('U', 'V') "
                "  AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "ORDER BY s.name, o.name;"
            )
            table_rows = cursor.fetchall()

            # 3. Columns
            cursor.execute(
                "SELECT "
                "  s.name AS schema_name, "
                "  o.name AS table_name, "
                "  c.name AS column_name, "
                "  t.name + CASE "
                "    WHEN t.name IN ('varchar', 'char', 'varbinary', 'binary') THEN "
                "      '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length AS VARCHAR(10)) END + ')' "
                "    WHEN t.name IN ('nvarchar', 'nchar') THEN "
                "      '(' + CASE WHEN c.max_length = -1 THEN 'max' ELSE CAST(c.max_length / 2 AS VARCHAR(10)) END + ')' "
                "    WHEN t.name IN ('decimal', 'numeric') THEN "
                "      '(' + CAST(c.precision AS VARCHAR(10)) + ',' + CAST(c.scale AS VARCHAR(10)) + ')' "
                "    ELSE '' "
                "  END AS data_type, "
                "  c.is_nullable, "
                "  dc.definition AS default_value, "
                "  c.column_id AS ordinal_position "
                "FROM sys.columns c "
                "JOIN sys.objects o ON c.object_id = o.object_id "
                "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                "JOIN sys.types t ON c.user_type_id = t.user_type_id "
                "LEFT JOIN sys.default_constraints dc ON c.default_object_id = dc.object_id "
                "WHERE o.type IN ('U', 'V') "
                "  AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "ORDER BY s.name, o.name, c.column_id;"
            )
            cols_by_table: dict[tuple[str, str], list[ColumnMetadata]] = {}
            for s_name, t_name, c_name, c_type, is_null, c_def, ord_pos in cursor.fetchall():
                cols_by_table.setdefault((s_name, t_name), []).append(
                    ColumnMetadata(
                        name=c_name,
                        data_type=c_type,
                        nullable=bool(is_null),
                        default=c_def,
                        ordinal_position=ord_pos,
                    )
                )

            # 4. PK and Unique constraints
            cursor.execute(
                "SELECT "
                "  kc.name AS constraint_name, "
                "  kc.type_desc AS constraint_type, "
                "  s.name AS schema_name, "
                "  t.name AS table_name, "
                "  c.name AS column_name "
                "FROM sys.key_constraints kc "
                "JOIN sys.tables t ON kc.parent_object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN sys.indexes i ON kc.parent_object_id = i.object_id AND kc.unique_index_id = i.index_id "
                "JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                "JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "ORDER BY s.name, t.name, kc.name, ic.key_ordinal;"
            )
            raw_constraints: dict[tuple[str, str, str, str], list[str]] = {}
            for c_name, c_type, s_name, t_name, col in cursor.fetchall():
                raw_constraints.setdefault((s_name, t_name, c_name, c_type), []).append(col)

            pk_by_table: dict[tuple[str, str], ConstraintMetadata] = {}
            uk_by_table: dict[tuple[str, str], list[ConstraintMetadata]] = {}
            for (s_name, t_name, c_name, c_type), cols in raw_constraints.items():
                is_pk = "PRIMARY" in c_type.upper()
                c_meta = ConstraintMetadata(
                    name=c_name,
                    columns=tuple(cols),
                    constraint_type="PRIMARY KEY" if is_pk else "UNIQUE",
                )
                if is_pk:
                    pk_by_table[(s_name, t_name)] = c_meta
                else:
                    uk_by_table.setdefault((s_name, t_name), []).append(c_meta)

            # 5. Foreign keys
            cursor.execute(
                "SELECT "
                "  fk.name AS fk_name, "
                "  s_src.name AS src_schema, "
                "  t_src.name AS src_table, "
                "  c_src.name AS src_col, "
                "  s_tgt.name AS tgt_schema, "
                "  t_tgt.name AS tgt_table, "
                "  c_tgt.name AS tgt_col, "
                "  fk.delete_referential_action_desc, "
                "  fk.update_referential_action_desc "
                "FROM sys.foreign_keys fk "
                "JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id "
                "JOIN sys.tables t_src ON fkc.parent_object_id = t_src.object_id "
                "JOIN sys.schemas s_src ON t_src.schema_id = s_src.schema_id "
                "JOIN sys.columns c_src ON fkc.parent_object_id = c_src.object_id AND fkc.parent_column_id = c_src.column_id "
                "JOIN sys.tables t_tgt ON fkc.referenced_object_id = t_tgt.object_id "
                "JOIN sys.schemas s_tgt ON t_tgt.schema_id = s_tgt.schema_id "
                "JOIN sys.columns c_tgt ON fkc.referenced_object_id = c_tgt.object_id AND fkc.referenced_column_id = c_tgt.column_id "
                "WHERE s_src.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "ORDER BY s_src.name, t_src.name, fk.name, fkc.constraint_column_id;"
            )
            raw_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
            for fk_name, s_schema, s_table, s_col, t_schema, t_table, t_col, del_act, upd_act in cursor.fetchall():
                key = (s_schema, s_table, fk_name)
                if key not in raw_fks:
                    raw_fks[key] = {
                        "name": fk_name,
                        "src_schema": s_schema,
                        "src_table": s_table,
                        "src_cols": [],
                        "tgt_schema": t_schema,
                        "tgt_table": t_table,
                        "tgt_cols": [],
                        "on_delete": del_act if del_act != "NO_ACTION" else None,
                        "on_update": upd_act if upd_act != "NO_ACTION" else None,
                    }
                raw_fks[key]["src_cols"].append(s_col)
                raw_fks[key]["tgt_cols"].append(t_col)

            fks_by_table: dict[tuple[str, str], list[ForeignKeyMetadata]] = {}
            all_fks: list[ForeignKeyMetadata] = []
            for (s_schema, s_table, _), fk_data in raw_fks.items():
                fk_obj = ForeignKeyMetadata(
                    name=fk_data["name"],
                    source_schema=fk_data["src_schema"],
                    source_table=fk_data["src_table"],
                    source_columns=tuple(fk_data["src_cols"]),
                    target_schema=fk_data["tgt_schema"],
                    target_table=fk_data["tgt_table"],
                    target_columns=tuple(fk_data["tgt_cols"]),
                    on_delete=fk_data["on_delete"],
                    on_update=fk_data["on_update"],
                )
                fks_by_table.setdefault((s_schema, s_table), []).append(fk_obj)
                all_fks.append(fk_obj)

            # 6. Indexes
            cursor.execute(
                "SELECT "
                "  s.name AS schema_name, "
                "  t.name AS table_name, "
                "  i.name AS index_name, "
                "  c.name AS column_name, "
                "  i.is_unique, "
                "  i.is_primary_key, "
                "  i.type_desc AS index_type, "
                "  ic.is_included_column, "
                "  ic.is_descending_key, "
                "  i.filter_definition "
                "FROM sys.indexes i "
                "JOIN sys.tables t ON i.object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
                "JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "  AND i.name IS NOT NULL "
                "ORDER BY s.name, t.name, i.name, ic.key_ordinal, ic.index_column_id;"
            )
            raw_idx: dict[tuple[str, str, str], dict[str, Any]] = {}
            for s_name, t_name, i_name, col_name, is_unique, is_pk, i_type, is_inc, is_desc, filter_def in cursor.fetchall():
                key = (s_name, t_name, i_name)
                if key not in raw_idx:
                    raw_idx[key] = {
                        "name": i_name,
                        "cols": [],
                        "orders": [],
                        "included": [],
                        "unique": bool(is_unique),
                        "is_primary": bool(is_pk),
                        "method": i_type,
                        "filter": filter_def,
                    }
                if is_inc:
                    raw_idx[key]["included"].append(col_name)
                else:
                    raw_idx[key]["cols"].append(col_name)
                    raw_idx[key]["orders"].append("DESC" if is_desc else "ASC")

            indexes_by_table: dict[tuple[str, str], list[IndexMetadata]] = {}
            for (s_name, t_name, _), idx_data in raw_idx.items():
                indexes_by_table.setdefault((s_name, t_name), []).append(
                    IndexMetadata(
                        name=idx_data["name"],
                        columns=tuple(idx_data["cols"]),
                        column_orders=tuple(idx_data["orders"]),
                        included_columns=tuple(idx_data["included"]),
                        unique=idx_data["unique"],
                        is_primary=idx_data["is_primary"],
                        method=idx_data["method"],
                        filter_condition=idx_data["filter"],
                    )
                )

            # 7. Check constraints
            cursor.execute(
                "SELECT s.name AS schema_name, t.name AS table_name, cc.name AS constraint_name, cc.definition AS check_clause "
                "FROM sys.check_constraints cc "
                "JOIN sys.tables t ON cc.parent_object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA') "
                "ORDER BY s.name, t.name, cc.name;"
            )
            checks_by_table: dict[tuple[str, str], list[ConstraintMetadata]] = {}
            for s_name, t_name, c_name, c_def in cursor.fetchall():
                checks_by_table.setdefault((s_name, t_name), []).append(
                    ConstraintMetadata(
                        name=c_name,
                        columns=(),
                        constraint_type="CHECK",
                        check_clause=c_def,
                    )
                )

            tables: list[TableMetadata] = []
            for s_name, t_name, t_type in table_rows:
                key = (s_name, t_name)
                is_view = "VIEW" in t_type.upper()
                tables.append(
                    TableMetadata(
                        name=t_name,
                        table_type=TableType.VIEW if is_view else TableType.TABLE,
                        schema_name=s_name,
                        columns=tuple(cols_by_table.get(key, [])),
                        primary_key=pk_by_table.get(key),
                        unique_constraints=tuple(uk_by_table.get(key, [])),
                        foreign_keys=tuple(fks_by_table.get(key, [])),
                        check_constraints=tuple(checks_by_table.get(key, [])),
                        indexes=tuple(indexes_by_table.get(key, [])),
                    )
                )

            return DatabaseSchema(
                database_name=connection.database,
                engine="SQL Server",
                schemas=schemas,
                tables=tuple(tables),
                relationships=tuple(all_fks),
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def introspect_table(
        self,
        connection: SQLServerConnection,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single Microsoft SQL Server table or view."""
        table_name = unquote_identifier(table_name)
        if schema:
            schema = unquote_identifier(schema)
        full_schema = self.introspect_schema(connection, password=password)
        tbl = full_schema.get_table(table_name, schema=schema)
        if tbl is None:
            target = f"{schema}.{table_name}" if schema else table_name
            raise ObjectNotFoundError(f"Object '{target}' not found in database '{connection.database}'.")
        return tbl
