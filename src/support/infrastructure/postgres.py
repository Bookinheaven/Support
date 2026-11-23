"""PostgreSQL connectivity and schema introspection implementation using Psycopg 3."""

from __future__ import annotations

import re
from typing import Any
import psycopg
import psycopg.errors

from support.application.errors import (
    AmbiguousObjectNameError,
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    ObjectNotFoundError,
    SSLVerificationError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import PostgresConnection
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

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


def _parse_pg_indexdef(i_def: str) -> tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]:
    """Parse PostgreSQL pg_indexes.indexdef for method, key columns, directions, included columns, and WHERE filter."""
    method_match = re.search(r"USING\s+(\w+)", i_def, re.IGNORECASE)
    method = method_match.group(1).upper() if method_match else None

    where_match = re.search(r"\s+WHERE\s+(.+)$", i_def, re.IGNORECASE)
    filter_cond = where_match.group(1).strip() if where_match else None

    include_match = re.search(r"\s+INCLUDE\s*\(([^)]+)\)", i_def, re.IGNORECASE)
    included = tuple(c.strip().strip('"') for c in include_match.group(1).split(",")) if include_match else ()

    core = i_def
    if where_match:
        core = core[:where_match.start()]
    if include_match:
        core = core[:include_match.start()]

    p_match = re.search(r"\((.+)\)$", core.strip())
    cols: list[str] = []
    orders: list[str] = []
    if p_match:
        raw = p_match.group(1).strip()
        depth = 0
        cur_col: list[str] = []
        raw_cols: list[str] = []
        for ch in raw:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                raw_cols.append("".join(cur_col).strip())
                cur_col = []
            else:
                cur_col.append(ch)
        if cur_col:
            raw_cols.append("".join(cur_col).strip())

        for rc in raw_cols:
            order = "ASC"
            if re.search(r"\s+DESC(?:\s+NULLS\s+(?:FIRST|LAST))?$", rc, re.IGNORECASE):
                order = "DESC"
                col_clean = re.sub(r"\s+DESC(?:\s+NULLS\s+(?:FIRST|LAST))?$", "", rc, flags=re.IGNORECASE).strip()
            elif re.search(r"\s+ASC(?:\s+NULLS\s+(?:FIRST|LAST))?$", rc, re.IGNORECASE):
                order = "ASC"
                col_clean = re.sub(r"\s+ASC(?:\s+NULLS\s+(?:FIRST|LAST))?$", "", rc, flags=re.IGNORECASE).strip()
            else:
                col_clean = rc
            cols.append(col_clean.strip('"'))
            orders.append(order)

    return method, tuple(cols), tuple(orders), included, filter_cond


class PostgresConnector:
    """Infrastructure connector for establishing, testing, and introspecting PostgreSQL databases."""

    def __init__(self, connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS) -> None:
        self._connect_timeout = connect_timeout

    def _translate_operational_error(
        self,
        exc: psycopg.OperationalError,
        connection: PostgresConnection,
    ) -> None:
        """Translate a psycopg.OperationalError into a specific application ConnectionError."""
        sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        err_msg = str(exc).lower()

        # Authentication failure
        if sqlstate in ("28P01", "28000") or "password authentication failed" in err_msg or "authentication failed" in err_msg:
            raise AuthenticationFailedError(
                f"Authentication failed for user '{connection.username}'.",
                used_saved_password=False,
            ) from exc

        # Database does not exist
        if sqlstate == "3D000" or ("does not exist" in err_msg and "database" in err_msg):
            raise DatabaseNotFoundError(
                f"Database '{connection.database}' does not exist on host '{connection.host}:{connection.port}'."
            ) from exc

        # Timeout
        if "timeout" in err_msg or "timed out" in err_msg:
            raise ConnectionTimeoutError(
                f"Connection timed out after {self._connect_timeout} seconds connecting to '{connection.host}:{connection.port}'."
            ) from exc

        # Connection refused
        if "connection refused" in err_msg:
            raise HostUnreachableError(
                f"Connection refused by '{connection.host}:{connection.port}'. "
                "Ensure PostgreSQL is running and accepting TCP connections."
            ) from exc

        # Hostname resolution failure
        if any(
            hint in err_msg
            for hint in (
                "name or service not known",
                "could not translate host name",
                "nodename nor servname provided",
                "temporary failure in name resolution",
            )
        ):
            raise HostUnreachableError(
                f"Could not resolve host '{connection.host}'."
            ) from exc

        # SSL failure
        if "ssl" in err_msg or "tls" in err_msg or "certificate" in err_msg:
            raise SSLVerificationError(
                f"SSL/TLS verification failed connecting to '{connection.host}:{connection.port}' with ssl_mode='{connection.ssl_mode}'."
            ) from exc

        # Network unreachable
        if "network is unreachable" in err_msg or "no route to host" in err_msg:
            raise HostUnreachableError(
                f"Network unreachable connecting to '{connection.host}:{connection.port}'."
            ) from exc

        # General failure fallback
        raise GeneralConnectionError(
            f"Failed to connect to '{connection.host}:{connection.port}': {exc}"
        ) from exc

    def _open_connection(
        self,
        connection: PostgresConnection,
        password: str | None = None,
    ) -> psycopg.Connection:
        """Open a PostgreSQL connection with exact parameters."""
        if not isinstance(connection, PostgresConnection):
            raise TypeError(
                f"PostgresConnector requires PostgresConnection, got {type(connection).__name__}"
            )

        params: dict[str, Any] = {
            "host": connection.host,
            "port": connection.port,
            "dbname": connection.database,
            "user": connection.username,
            "sslmode": connection.ssl_mode,
            "connect_timeout": self._connect_timeout,
        }
        if password is not None:
            params["password"] = password

        try:
            return psycopg.connect(**params)
        except (
            psycopg.errors.InvalidPassword,
            psycopg.errors.InvalidAuthorizationSpecification,
        ) as exc:
            raise AuthenticationFailedError(
                f"Authentication failed for user '{connection.username}'.",
                used_saved_password=False,
            ) from exc
        except psycopg.errors.InvalidCatalogName as exc:
            raise DatabaseNotFoundError(
                f"Database '{connection.database}' does not exist on host '{connection.host}:{connection.port}'."
            ) from exc
        except psycopg.OperationalError as exc:
            self._translate_operational_error(exc, connection)
            raise
        except psycopg.Error as exc:
            raise GeneralConnectionError(f"PostgreSQL error: {exc}") from exc

    def test_connection(
        self,
        connection: PostgresConnection,
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
            except psycopg.OperationalError as exc:
                self._translate_operational_error(exc, connection)
                raise
            except psycopg.Error as exc:
                raise GeneralConnectionError(f"PostgreSQL error: {exc}") from exc

            server_version: str | None = None
            if hasattr(conn, "info"):
                server_version = conn.info.parameter_status("server_version")
                if not server_version and conn.info.server_version:
                    v = conn.info.server_version
                    major = v // 10000
                    minor = v % 100
                    server_version = f"{major}.{minor}"

            return ConnectionTestResult(
                success=True,
                database_type="postgresql",
                database=connection.database,
                username=connection.username,
                host=connection.host,
                port=connection.port,
                ssl_mode=connection.ssl_mode,
                server_version=server_version,
                message="Connection successful.",
            )
        finally:
            if conn is not None and not conn.closed:
                try:
                    conn.close()
                except Exception:
                    pass

    def introspect_schema(
        self,
        connection: PostgresConnection,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect full PostgreSQL database schema."""
        conn = self._open_connection(connection, password=password)
        try:
            with conn.cursor() as cur:
                # 1. Schemas
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "  AND schema_name NOT LIKE 'pg_temp%' "
                    "ORDER BY schema_name;"
                )
                schemas = tuple(r[0] for r in cur.fetchall())

                # 2. Tables and views
                cur.execute(
                    "SELECT table_schema, table_name, table_type "
                    "FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "  AND table_schema NOT LIKE 'pg_temp%' "
                    "ORDER BY table_schema, table_name;"
                )
                table_rows = cur.fetchall()

                # 3. Columns
                cur.execute(
                    "SELECT "
                    "  table_schema, table_name, column_name, "
                    "  CASE "
                    "    WHEN data_type = 'USER-DEFINED' THEN udt_name "
                    "    WHEN character_maximum_length IS NOT NULL THEN data_type || '(' || character_maximum_length || ')' "
                    "    WHEN numeric_precision IS NOT NULL AND data_type = 'numeric' THEN "
                    "      data_type || '(' || numeric_precision || (CASE WHEN numeric_scale IS NOT NULL THEN ',' || numeric_scale ELSE '' END) || ')' "
                    "    ELSE data_type "
                    "  END AS formatted_type, "
                    "  is_nullable, column_default, ordinal_position "
                    "FROM information_schema.columns "
                    "WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "  AND table_schema NOT LIKE 'pg_temp%' "
                    "ORDER BY table_schema, table_name, ordinal_position;"
                )
                col_rows = cur.fetchall()
                cols_by_table: dict[tuple[str, str], list[ColumnMetadata]] = {}
                for r in col_rows:
                    cols_by_table.setdefault((r[0], r[1]), []).append(
                        ColumnMetadata(
                            name=r[2],
                            data_type=r[3],
                            nullable=(r[4] == "YES"),
                            default=r[5],
                            ordinal_position=r[6],
                        )
                    )

                # 4. PK and Unique constraints
                cur.execute(
                    "SELECT tc.constraint_name, tc.constraint_type, tc.table_schema, tc.table_name, kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name "
                    " AND tc.table_schema = kcu.table_schema "
                    "WHERE tc.table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE') "
                    "ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position;"
                )
                pk_by_table: dict[tuple[str, str], ConstraintMetadata] = {}
                uk_by_table: dict[tuple[str, str], list[ConstraintMetadata]] = {}
                raw_constraints: dict[tuple[str, str, str, str], list[str]] = {}
                for c_name, c_type, t_schema, t_name, col in cur.fetchall():
                    raw_constraints.setdefault((t_schema, t_name, c_name, c_type), []).append(col)

                for (t_schema, t_name, c_name, c_type), cols in raw_constraints.items():
                    c_meta = ConstraintMetadata(name=c_name, columns=tuple(cols), constraint_type=c_type)
                    if c_type == "PRIMARY KEY":
                        pk_by_table[(t_schema, t_name)] = c_meta
                    else:
                        uk_by_table.setdefault((t_schema, t_name), []).append(c_meta)

                # 5. Foreign keys
                cur.execute(
                    "SELECT "
                    "  tc.constraint_name, tc.table_schema, tc.table_name, kcu.column_name, "
                    "  ccu.table_schema AS tgt_schema, ccu.table_name AS tgt_table, ccu.column_name AS tgt_col, "
                    "  rc.delete_rule, rc.update_rule "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                    "JOIN information_schema.referential_constraints rc "
                    "  ON tc.constraint_name = rc.constraint_name AND tc.table_schema = rc.constraint_schema "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.constraint_schema "
                    "WHERE tc.constraint_type = 'FOREIGN KEY' "
                    "  AND tc.table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "ORDER BY tc.table_schema, tc.table_name, tc.constraint_name, kcu.ordinal_position;"
                )
                raw_fks: dict[tuple[str, str, str], dict[str, Any]] = {}
                for c_name, s_schema, s_tbl, s_col, t_schema, t_tbl, t_col, del_rule, upd_rule in cur.fetchall():
                    key = (s_schema, s_tbl, c_name)
                    if key not in raw_fks:
                        raw_fks[key] = {
                            "name": c_name,
                            "src_schema": s_schema,
                            "src_table": s_tbl,
                            "src_cols": [],
                            "tgt_schema": t_schema,
                            "tgt_table": t_tbl,
                            "tgt_cols": [],
                            "on_delete": del_rule if del_rule != "NO ACTION" else None,
                            "on_update": upd_rule if upd_rule != "NO ACTION" else None,
                        }
                    raw_fks[key]["src_cols"].append(s_col)
                    raw_fks[key]["tgt_cols"].append(t_col)

                fks_by_table: dict[tuple[str, str], list[ForeignKeyMetadata]] = {}
                all_fks: list[ForeignKeyMetadata] = []
                for (s_schema, s_tbl, _), fk_data in raw_fks.items():
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
                    fks_by_table.setdefault((s_schema, s_tbl), []).append(fk_obj)
                    all_fks.append(fk_obj)

                # 6. Check constraints
                cur.execute(
                    "SELECT tc.table_schema, tc.table_name, tc.constraint_name, cc.check_clause "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.check_constraints cc "
                    "  ON tc.constraint_name = cc.constraint_name AND tc.constraint_schema = cc.constraint_schema "
                    "WHERE tc.constraint_type = 'CHECK' "
                    "  AND tc.table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "ORDER BY tc.table_schema, tc.table_name, tc.constraint_name;"
                )
                checks_by_table: dict[tuple[str, str], list[ConstraintMetadata]] = {}
                for t_schema, t_name, c_name, c_clause in cur.fetchall():
                    checks_by_table.setdefault((t_schema, t_name), []).append(
                        ConstraintMetadata(
                            name=c_name,
                            columns=(),
                            constraint_type="CHECK",
                            check_clause=c_clause,
                        )
                    )

                # 7. Indexes
                cur.execute(
                    "SELECT schemaname, tablename, indexname, indexdef "
                    "FROM pg_indexes "
                    "WHERE schemaname NOT IN ('information_schema', 'pg_catalog', 'pg_toast') "
                    "  AND schemaname NOT LIKE 'pg_temp%' "
                    "ORDER BY schemaname, tablename, indexname;"
                )
                indexes_by_table: dict[tuple[str, str], list[IndexMetadata]] = {}
                for s_name, t_name, i_name, i_def in cur.fetchall():
                    is_unique = "UNIQUE" in i_def.upper()
                    method, i_cols, i_orders, inc_cols, filter_cond = _parse_pg_indexdef(i_def)
                    indexes_by_table.setdefault((s_name, t_name), []).append(
                        IndexMetadata(
                            name=i_name,
                            columns=i_cols,
                            unique=is_unique,
                            is_primary=False,
                            method=method,
                            column_orders=i_orders,
                            included_columns=inc_cols,
                            filter_condition=filter_cond,
                        )
                    )

                tables: list[TableMetadata] = []
                for s_name, t_name, t_type in table_rows:
                    key = (s_name, t_name)
                    tables.append(
                        TableMetadata(
                            name=t_name,
                            table_type=TableType.VIEW if t_type == "VIEW" else TableType.TABLE,
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
                    engine="PostgreSQL",
                    schemas=schemas,
                    tables=tuple(tables),
                    relationships=tuple(all_fks),
                )
        finally:
            if not conn.closed:
                conn.close()

    def introspect_table(
        self,
        connection: PostgresConnection,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a single PostgreSQL table or view."""
        clean_name = unquote_identifier(table_name)
        clean_schema = unquote_identifier(schema) if schema else None
        full_schema = self.introspect_schema(connection, password=password)
        tbl = full_schema.get_table(clean_name, schema=clean_schema)
        if tbl is None:
            target = f"{schema}.{table_name}" if schema else table_name
            raise ObjectNotFoundError(f"Object '{target}' not found in database '{connection.database}'.")
        return tbl
