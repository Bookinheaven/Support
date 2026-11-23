"""MySQL and MariaDB connectivity implementation using PyMySQL."""

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
    SSLVerificationError,
)
from support.application.ports import ConnectionTestResult
from support.domain.profiles import (
    DatabaseType,
    MariaDBConnection,
    MySQLConnection,
    ProfileValidationError,
)

DEFAULT_MYSQL_TIMEOUT_SECONDS: float = 10.0


def _build_ssl_params(ssl_mode: str) -> dict[str, Any]:
    """Map MySQL and MariaDB domain SSL mode strings to PyMySQL SSL parameters."""
    mode = ssl_mode.strip().lower()

    # 1. Disabled: plain unencrypted transport
    if mode in ("disabled",):
        return {"ssl_disabled": True, "ssl": None}

    # 2. Preferred: attempt TLS if server supports it, fallback gracefully to plain transport
    elif mode in ("preferred", "prefer"):
        return {"ssl_disabled": False, "ssl": None}

    # 3. Required: require TLS with no plaintext fallback, but skip CA/host verification
    elif mode in ("required", "require"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return {"ssl": ctx}

    # 4. Verify CA: require TLS and validate server certificate against trusted system CAs
    elif mode in ("verify_ca", "verify-ca"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return {"ssl": ctx}

    # 5. Verify Identity / Full: require TLS, validate certificate against trusted CAs, AND verify hostname
    elif mode in ("verify_identity", "verify-full"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return {"ssl": ctx}

    else:
        raise ProfileValidationError(f"Unsupported SSL mode '{ssl_mode}'.")


class MySQLCompatibleConnector:
    """Infrastructure connector for testing MySQL and MariaDB database connections using PyMySQL."""

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

        # Fallback to general error without exposing password or full internal parameters
        raise GeneralConnectionError(
            f"Failed to connect to '{connection.host}:{connection.port}': {exc}"
        ) from exc

    def test_connection(
        self,
        connection: MySQLConnection | MariaDBConnection,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Establish connection with exact parameters, run SELECT 1, and return result."""
        if not isinstance(connection, (MySQLConnection, MariaDBConnection)):
            raise TypeError(
                f"MySQLCompatibleConnector requires MySQLConnection or MariaDBConnection, got {type(connection).__name__}"
            )

        ssl_params = _build_ssl_params(connection.ssl_mode)

        # Map non-secret parameters individually; never build a URI/DSN containing credentials
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

        conn = None
        try:
            conn = pymysql.connect(**connect_kwargs)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row is None or row[0] != 1:
                    raise GeneralConnectionError(
                        f"Database verification query failed: expected 1, got {row}"
                    )

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
        except pymysql.MySQLError as exc:
            self._translate_error(exc, connection)
            # Satisfy type checker as _translate_error always raises
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ConnectionTimeoutError(
                f"Connection timed out after {int(self._timeout_seconds)} seconds connecting to '{connection.host}:{connection.port}'."
            ) from exc
        except Exception as exc:
            raise GeneralConnectionError(
                f"Failed to connect to '{connection.host}:{connection.port}': {exc}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
