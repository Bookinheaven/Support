"""PostgreSQL connectivity implementation using Psycopg 3."""

from __future__ import annotations

from typing import Any
import psycopg
import psycopg.errors

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    SSLVerificationError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import PostgresConnection

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


class PostgresConnector:
    """Infrastructure connector for establishing and verifying PostgreSQL connections."""

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

        # SSL/TLS verification failure
        if any(hint in err_msg for hint in ("ssl", "certificate", "tls")):
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

    def test_connection(
        self,
        connection: PostgresConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Establish connection with exact parameters, run SELECT 1, and return result."""
        if not isinstance(connection, PostgresConnection):
            raise TypeError(
                f"PostgresConnector requires PostgresConnection, got {type(connection).__name__}"
            )

        # Individual keyword parameters; never build a DSN string with secrets
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

        conn = None
        try:
            conn = psycopg.connect(**params)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row is None or row[0] != 1:
                    raise GeneralConnectionError(
                        f"Database verification query failed: expected 1, got {row}"
                    )

            # Retrieve server version without executing extra queries
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
            # _translate_operational_error always raises, but satisfies type checker
            raise
        except psycopg.Error as exc:
            raise GeneralConnectionError(f"PostgreSQL error: {exc}") from exc
        finally:
            if conn is not None and not conn.closed:
                try:
                    conn.close()
                except Exception:
                    pass
