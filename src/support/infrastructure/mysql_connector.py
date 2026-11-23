"""MySQL and MariaDB connectivity and schema introspection implementation using PyMySQL."""

from __future__ import annotations

import socket
import ssl
from typing import Any
import pymysql
import pymysql.constants.CR as CR
import pymysql.constants.ER as ER

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    ObjectNotFoundError,
    SSLVerificationError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import (
    DatabaseType,
    MariaDBConnection,
    MySQLConnection,
    ProfileValidationError,
)
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

DEFAULT_MYSQL_TIMEOUT_SECONDS: float = 10.0


def _build_ssl_params(ssl_mode: str) -> dict[str, Any]:
    """Map MySQL and MariaDB domain SSL mode strings to PyMySQL SSL parameters."""
    mode = ssl_mode.strip().lower()

    if mode in ("disabled",):
        return {"ssl_disabled": True, "ssl": None}
    elif mode in ("preferred", "prefer"):
        return {"ssl_disabled": False, "ssl": None}
    elif mode in ("required", "require"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}
    elif mode in ("verify_ca", "verify-ca"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return {"ssl": ctx}
    elif mode in ("verify_identity", "verify-full"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return {"ssl": ctx}
    else:
        raise ProfileValidationError(f"Unsupported SSL mode '{ssl_mode}'.")


class MySQLCompatibleConnector:
    """Infrastructure connector for testing and introspecting MySQL and MariaDB databases using PyMySQL."""

    def __init__(self, timeout_seconds: float = DEFAULT_MYSQL_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    def _translate_error(
        self,
        exc: Exception,
        connection: MySQLConnection | MariaDBConnection,
    ) -> None:
        """Translate PyMySQL exceptions into specific application connection errors."""
        errno: int | None = None
        sqlstate: str | None = getattr(exc, "sqlstate", None)
        orig: BaseException | None = getattr(exc, "original_exception", None)

        if hasattr(exc, "args") and len(exc.args) > 0 and isinstance(exc.args[0], int):
            errno = exc.args[0]

        # 1. Authentication failure (vendor code 1045 or SQLSTATE 28000)
        # Note: Do not map 1044 here; 1044 indicates valid authentication but lacking database access privileges.
        if errno == ER.ACCESS_DENIED_ERROR or sqlstate == "28000":
            raise AuthenticationFailedError(
                f"Authentication failed for user '{connection.username}'.",
                used_saved_password=False,
            ) from exc

        # 2. Database access denied (vendor code 1044) - user authenticated, but lacks access to the database
        if errno == ER.DBACCESS_DENIED_ERROR:
            raise GeneralConnectionError(
                f"Access denied for user '{connection.username}' to database '{connection.database}'. "
                "The authenticated account lacks access privileges to this database."
            ) from exc

        # 3. Database not found (vendor code 1049)
        # Note: Do not map generic SQLSTATE 42000 to DatabaseNotFoundError.
        if errno == ER.BAD_DB_ERROR:
            raise DatabaseNotFoundError(
                f"Database '{connection.database}' does not exist on host '{connection.host}:{connection.port}'."
            ) from exc

        # 3. Hostname resolution failure
        if errno in (CR.CR_UNKNOWN_HOST, ER.BAD_HOST_ERROR) or isinstance(orig, socket.gaierror):
            raise HostUnreachableError(
                f"Could not resolve host '{connection.host}'."
            ) from exc

        # 4. Connection refused or connection host errors
        if errno in (CR.CR_CONN_HOST_ERROR, CR.CR_CONNECTION_ERROR):
            if isinstance(orig, (TimeoutError, socket.timeout)) or "timed out" in str(orig).lower():
                raise ConnectionTimeoutError(
                    f"Connection timed out after {int(self._timeout_seconds)} seconds connecting to '{connection.host}:{connection.port}'."
                ) from exc
            if isinstance(orig, ConnectionRefusedError) or "connection refused" in str(orig).lower():
                target_name = "MySQL" if connection.database_type == DatabaseType.MYSQL else "MariaDB"
                raise HostUnreachableError(
                    f"Connection refused by '{connection.host}:{connection.port}'. "
                    f"Ensure {target_name} is running and accepting TCP connections."
                ) from exc
            if isinstance(orig, socket.gaierror) or "name or service not known" in str(orig).lower():
                raise HostUnreachableError(
                    f"Could not resolve host '{connection.host}'."
                ) from exc
            if isinstance(orig, (ssl.SSLError, ssl.CertificateError)):
                raise SSLVerificationError(
                    f"SSL/TLS verification failed connecting to '{connection.host}:{connection.port}' with ssl_mode='{connection.ssl_mode}'."
                ) from exc
            if "network is unreachable" in str(orig).lower() or "no route to host" in str(orig).lower():
                raise HostUnreachableError(
                    f"Network unreachable connecting to '{connection.host}:{connection.port}'."
                ) from exc
            raise HostUnreachableError(
                f"Could not connect to database server at '{connection.host}:{connection.port}'."
            ) from exc

        # 5. SSL / TLS Verification Error
        if (
            errno == CR.CR_SSL_CONNECTION_ERROR
            or isinstance(orig, (ssl.SSLError, ssl.CertificateError))
            or any(hint in str(exc).lower() for hint in ("certificate verify failed", "ssl_error", "tls handshake"))
        ):
            raise SSLVerificationError(
                f"SSL/TLS verification failed connecting to '{connection.host}:{connection.port}' with ssl_mode='{connection.ssl_mode}'."
            ) from exc

        # 6. Timeout
        if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
            raise ConnectionTimeoutError(
                f"Connection timed out after {int(self._timeout_seconds)} seconds connecting to '{connection.host}:{connection.port}'."
            ) from exc

        raise GeneralConnectionError(
            f"Failed to connect to '{connection.host}:{connection.port}': {exc}"
        ) from exc

    def _open_connection(
        self,
        connection: MySQLConnection | MariaDBConnection,
        password: str | None = None,
    ) -> pymysql.Connection:
        """Open a PyMySQL connection with exact parameters."""
        if not isinstance(connection, (MySQLConnection, MariaDBConnection)):
            raise TypeError(
                f"MySQLCompatibleConnector requires MySQLConnection or MariaDBConnection, got {type(connection).__name__}"
            )

        ssl_params = _build_ssl_params(connection.ssl_mode)
        connect_kwargs: dict[str, Any] = {
            "host": connection.host,
            "port": connection.port,
            "database": connection.database,
            "user": connection.username,
            "password": password if password is not None else "",
            "connect_timeout": self._timeout_seconds,
            "read_timeout": self._timeout_seconds,
            "write_timeout": self._timeout_seconds,
            "local_infile": False,
            "read_default_file": None,
            "read_default_group": None,
            "autocommit": False,
            **ssl_params,
        }

        try:
            return pymysql.connect(**connect_kwargs)
        except pymysql.MySQLError as exc:
            self._translate_error(exc, connection)
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ConnectionTimeoutError(
                f"Connection timed out after {int(self._timeout_seconds)} seconds connecting to '{connection.host}:{connection.port}'."
            ) from exc
        except Exception as exc:
            raise GeneralConnectionError(
                f"Failed to connect to '{connection.host}:{connection.port}': {exc}"
            ) from exc

    def test_connection(
        self,
        connection: MySQLConnection | MariaDBConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Establish connection with exact parameters, run SELECT 1, and return result."""
        conn = None
        try:
            conn = self._open_connection(connection, password=password)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    row = cur.fetchone()
                    if row is None or row[0] != 1:
                        raise GeneralConnectionError(
                            f"Database verification query failed: expected 1, got {row}"
                        )
            except Exception as exc:
                if isinstance(exc, (GeneralConnectionError, ConnectionTimeoutError, AuthenticationFailedError, DatabaseNotFoundError, HostUnreachableError, SSLVerificationError)):
                    raise
                if isinstance(exc, pymysql.MySQLError):
                    self._translate_error(exc, connection)
                raise GeneralConnectionError(f"Failed to execute query: {exc}") from exc

            server_version: str | None = None
            if hasattr(conn, "get_server_info"):
                server_version = conn.get_server_info()

            return ConnectionTestResult(
                success=True,
                database_type=connection.database_type.value,
                database=connection.database,
                username=connection.username,
                host=connection.host,
                port=connection.port,
                ssl_mode=connection.ssl_mode,
                server_version=server_version,
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
        connection: MySQLConnection | MariaDBConnection,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect full MySQL/MariaDB database schema."""
        conn = self._open_connection(connection, password=password)
        try:
            with conn.cursor() as cur:
                db_name = connection.database

                # 1. Tables and views
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE "
                    "FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME;",
                    (db_name,),
                )
                table_rows = cur.fetchall()

                # 2. Columns
                cur.execute(
                    "SELECT "
                    "  TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION;",
                    (db_name,),
                )
                cols_by_table: dict[str, list[ColumnMetadata]] = {}
                for t_name, c_name, c_type, is_null, c_def, ord_pos in cur.fetchall():
                    cols_by_table.setdefault(t_name, []).append(
                        ColumnMetadata(
                            name=c_name,
                            data_type=c_type,
                            nullable=(is_null == "YES"),
                            default=c_def,
                            ordinal_position=ord_pos,
                        )
                    )

                # 3. PK and Unique constraints
                cur.execute(
                    "SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, tc.TABLE_NAME, kcu.COLUMN_NAME "
                    "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                    "  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                    " AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA "
                    " AND tc.TABLE_NAME = kcu.TABLE_NAME "
                    "WHERE tc.TABLE_SCHEMA = %s "
                    "  AND tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE') "
                    "ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;",
                    (db_name,),
                )
                raw_constraints: dict[tuple[str, str, str], list[str]] = {}
                for c_name, c_type, t_name, col in cur.fetchall():
                    raw_constraints.setdefault((t_name, c_name, c_type), []).append(col)

                pk_by_table: dict[str, ConstraintMetadata] = {}
                uk_by_table: dict[str, list[ConstraintMetadata]] = {}
                for (t_name, c_name, c_type), cols in raw_constraints.items():
                    c_meta = ConstraintMetadata(name=c_name, columns=tuple(cols), constraint_type=c_type)
                    if c_type == "PRIMARY KEY":
                        pk_by_table[t_name] = c_meta
                    else:
                        uk_by_table.setdefault(t_name, []).append(c_meta)

                # 4. Foreign keys
                cur.execute(
                    "SELECT "
                    "  kcu.CONSTRAINT_NAME, kcu.TABLE_NAME, kcu.COLUMN_NAME, "
                    "  kcu.REFERENCED_TABLE_SCHEMA, kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME, "
                    "  rc.DELETE_RULE, rc.UPDATE_RULE "
                    "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu "
                    "JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc "
                    "  ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME AND kcu.TABLE_SCHEMA = rc.CONSTRAINT_SCHEMA "
                    "WHERE kcu.TABLE_SCHEMA = %s "
                    "  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL "
                    "ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION;",
                    (db_name,),
                )
                raw_fks: dict[tuple[str, str], dict[str, Any]] = {}
                for c_name, s_tbl, s_col, t_schema, t_tbl, t_col, del_rule, upd_rule in cur.fetchall():
                    key = (s_tbl, c_name)
                    if key not in raw_fks:
                        raw_fks[key] = {
                            "name": c_name,
                            "src_table": s_tbl,
                            "src_cols": [],
                            "tgt_schema": t_schema if t_schema != db_name else None,
                            "tgt_table": t_tbl,
                            "tgt_cols": [],
                            "on_delete": del_rule if del_rule != "NO ACTION" else None,
                            "on_update": upd_rule if upd_rule != "NO ACTION" else None,
                        }
                    raw_fks[key]["src_cols"].append(s_col)
                    raw_fks[key]["tgt_cols"].append(t_col)

                fks_by_table: dict[str, list[ForeignKeyMetadata]] = {}
                all_fks: list[ForeignKeyMetadata] = []
                for (s_tbl, _), fk_data in raw_fks.items():
                    fk_obj = ForeignKeyMetadata(
                        name=fk_data["name"],
                        source_table=fk_data["src_table"],
                        source_columns=tuple(fk_data["src_cols"]),
                        target_schema=fk_data["tgt_schema"],
                        target_table=fk_data["tgt_table"],
                        target_columns=tuple(fk_data["tgt_cols"]),
                        on_delete=fk_data["on_delete"],
                        on_update=fk_data["on_update"],
                    )
                    fks_by_table.setdefault(s_tbl, []).append(fk_obj)
                    all_fks.append(fk_obj)

                # 5. Indexes
                cur.execute(
                    "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE, INDEX_TYPE, COLLATION "
                    "FROM INFORMATION_SCHEMA.STATISTICS "
                    "WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX;",
                    (db_name,),
                )
                raw_idx: dict[tuple[str, str], dict[str, Any]] = {}
                for t_name, i_name, col_name, non_unique, i_type, collation in cur.fetchall():
                    key = (t_name, i_name)
                    if key not in raw_idx:
                        raw_idx[key] = {
                            "name": i_name,
                            "cols": [],
                            "orders": [],
                            "unique": (non_unique == 0),
                            "is_primary": (i_name == "PRIMARY"),
                            "method": i_type,
                        }
                    raw_idx[key]["cols"].append(col_name)
                    raw_idx[key]["orders"].append("DESC" if collation == "D" else "ASC")

                indexes_by_table: dict[str, list[IndexMetadata]] = {}
                for (t_name, _), idx_data in raw_idx.items():
                    indexes_by_table.setdefault(t_name, []).append(
                        IndexMetadata(
                            name=idx_data["name"],
                            columns=tuple(idx_data["cols"]),
                            unique=idx_data["unique"],
                            is_primary=idx_data["is_primary"],
                            method=idx_data["method"],
                            column_orders=tuple(idx_data["orders"]),
                        )
                    )

                # 6. Check constraints (if supported by server version)
                checks_by_table: dict[str, list[ConstraintMetadata]] = {}
                try:
                    cur.execute(
                        "SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE "
                        "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
                        "JOIN INFORMATION_SCHEMA.CHECK_CONSTRAINTS cc "
                        "  ON tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = cc.CONSTRAINT_SCHEMA "
                        "WHERE tc.TABLE_SCHEMA = %s AND tc.CONSTRAINT_TYPE = 'CHECK';",
                        (db_name,),
                    )
                    for t_name, c_name, c_clause in cur.fetchall():
                        checks_by_table.setdefault(t_name, []).append(
                            ConstraintMetadata(
                                name=c_name,
                                columns=(),
                                constraint_type="CHECK",
                                check_clause=c_clause,
                            )
                        )
                except pymysql.MySQLError:
                    pass

                tables: list[TableMetadata] = []
                for t_name, t_type in table_rows:
                    tables.append(
                        TableMetadata(
                            name=t_name,
                            table_type=TableType.VIEW if t_type == "VIEW" else TableType.TABLE,
                            schema_name=None,
                            columns=tuple(cols_by_table.get(t_name, [])),
                            primary_key=pk_by_table.get(t_name),
                            unique_constraints=tuple(uk_by_table.get(t_name, [])),
                            foreign_keys=tuple(fks_by_table.get(t_name, [])),
                            check_constraints=tuple(checks_by_table.get(t_name, [])),
                            indexes=tuple(indexes_by_table.get(t_name, [])),
                        )
                    )

                engine_name = "MySQL" if isinstance(connection, MySQLConnection) else "MariaDB"
                return DatabaseSchema(
                    database_name=db_name,
                    engine=engine_name,
                    schemas=(),
                    tables=tuple(tables),
                    relationships=tuple(all_fks),
                )
        finally:
            conn.close()

    def introspect_table(
        self,
        connection: MySQLConnection | MariaDBConnection,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single MySQL/MariaDB table or view."""
        clean_name = unquote_identifier(table_name)
        full_schema = self.introspect_schema(connection, password=password)
        tbl = full_schema.get_table(clean_name, schema=schema)
        if tbl is None:
            raise ObjectNotFoundError(f"Object '{table_name}' not found in database '{connection.database}'.")
        return tbl
