"""Unit tests for MySQLCompatibleConnector infrastructure adapter."""

from __future__ import annotations

import socket
import ssl
from unittest.mock import MagicMock, patch
import pytest

pymysql = pytest.importorskip("pymysql")
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
from support.domain.profiles import (
    MariaDBConnection,
    MySQLConnection,
    PostgresConnection,
)
from support.infrastructure.mysql_connector import (
    DEFAULT_MYSQL_TIMEOUT_SECONDS,
    MySQLCompatibleConnector,
)


def _mock_successful_connection(server_version: str = "8.0.35") -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.get_server_info.return_value = server_version
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn


def test_mysql_connector_rejects_non_mysql_connection() -> None:
    connector = MySQLCompatibleConnector()
    pg_conn = PostgresConnection(database="db", username="u", ssl_mode="prefer")
    with pytest.raises(TypeError, match="requires MySQLConnection or MariaDBConnection"):
        connector.test_connection(pg_conn)  # type: ignore[arg-type]


def test_mysql_connector_parameters_mapping() -> None:
    connector = MySQLCompatibleConnector(timeout_seconds=15.0)
    conn_config = MySQLConnection(
        database="shop_db",
        username="admin",
        host="db.example.com",
        port=3307,
        ssl_mode="preferred",
    )
    mock_conn = _mock_successful_connection("8.4.0")

    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        result = connector.test_connection(conn_config, password="secret_password")

        assert result.success is True
        assert result.database_type == "mysql"
        assert result.database == "shop_db"
        assert result.username == "admin"
        assert result.host == "db.example.com"
        assert result.port == 3307
        assert result.ssl_mode == "preferred"
        assert result.server_version == "8.4.0"

        mock_connect.assert_called_once()
        kwargs = mock_connect.call_args.kwargs
        assert kwargs["host"] == "db.example.com"
        assert kwargs["port"] == 3307
        assert kwargs["database"] == "shop_db"
        assert kwargs["user"] == "admin"
        assert kwargs["password"] == "secret_password"
        assert kwargs["connect_timeout"] == 15.0
        assert kwargs["read_timeout"] == 15.0
        assert kwargs["write_timeout"] == 15.0
        assert kwargs["local_infile"] is False
        assert kwargs["read_default_file"] is None
        assert kwargs["read_default_group"] is None
        assert kwargs["autocommit"] is False
        assert kwargs["ssl_disabled"] is False
        assert kwargs["ssl"] is None


def test_mariadb_connector_parameters_mapping() -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MariaDBConnection(
        database="analytics",
        username="analyst",
        host="mariadb.local",
        port=3306,
        ssl_mode="prefer",
    )
    mock_conn = _mock_successful_connection("10.11.6-MariaDB")

    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        result = connector.test_connection(conn_config, password="maria_pass")

        assert result.success is True
        assert result.database_type == "mariadb"
        assert result.database == "analytics"
        assert result.server_version == "10.11.6-MariaDB"

        kwargs = mock_connect.call_args.kwargs
        assert kwargs["host"] == "mariadb.local"
        assert kwargs["database"] == "analytics"
        assert kwargs["password"] == "maria_pass"
        assert kwargs["ssl_disabled"] is False
        assert kwargs["ssl"] is None


@pytest.mark.parametrize(
    "mode,expected_disabled,expected_ssl_type,check_hostname,verify_mode",
    [
        ("disabled", True, None, False, ssl.CERT_NONE),
        ("preferred", False, None, False, ssl.CERT_NONE),
        ("required", False, ssl.SSLContext, False, ssl.CERT_NONE),
        ("verify_ca", False, ssl.SSLContext, False, ssl.CERT_REQUIRED),
        ("verify_identity", False, ssl.SSLContext, True, ssl.CERT_REQUIRED),
    ],
)
def test_mysql_ssl_mode_mapping(
    mode: str,
    expected_disabled: bool,
    expected_ssl_type: type | None,
    check_hostname: bool,
    verify_mode: ssl.VerifyMode,
) -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MySQLConnection(database="test", username="u", ssl_mode=mode)
    mock_conn = _mock_successful_connection()

    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn_config)
        kwargs = mock_connect.call_args.kwargs

        if expected_disabled:
            assert kwargs.get("ssl_disabled") is True
            assert kwargs.get("ssl") is None
        elif expected_ssl_type is None:
            assert kwargs.get("ssl_disabled") is False
            assert kwargs.get("ssl") is None
        else:
            ctx = kwargs.get("ssl")
            assert isinstance(ctx, ssl.SSLContext)
            assert ctx.check_hostname == check_hostname
            assert ctx.verify_mode == verify_mode


@pytest.mark.parametrize(
    "mode,expected_disabled,expected_ssl_type,check_hostname,verify_mode",
    [
        ("disabled", True, None, False, ssl.CERT_NONE),
        ("prefer", False, None, False, ssl.CERT_NONE),
        ("require", False, ssl.SSLContext, False, ssl.CERT_NONE),
        ("verify-ca", False, ssl.SSLContext, False, ssl.CERT_REQUIRED),
        ("verify-full", False, ssl.SSLContext, True, ssl.CERT_REQUIRED),
    ],
)
def test_mariadb_ssl_mode_mapping(
    mode: str,
    expected_disabled: bool,
    expected_ssl_type: type | None,
    check_hostname: bool,
    verify_mode: ssl.VerifyMode,
) -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MariaDBConnection(database="test", username="u", ssl_mode=mode)
    mock_conn = _mock_successful_connection()

    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn_config)
        kwargs = mock_connect.call_args.kwargs

        if expected_disabled:
            assert kwargs.get("ssl_disabled") is True
            assert kwargs.get("ssl") is None
        elif expected_ssl_type is None:
            assert kwargs.get("ssl_disabled") is False
            assert kwargs.get("ssl") is None
        else:
            ctx = kwargs.get("ssl")
            assert isinstance(ctx, ssl.SSLContext)
            assert ctx.check_hostname == check_hostname
            assert ctx.verify_mode == verify_mode


def test_mysql_connector_password_delivery_without_secrets() -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MySQLConnection(database="db", username="user")
    mock_conn = _mock_successful_connection()

    # With password
    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        res = connector.test_connection(conn_config, password="super_secret_password")
        assert res.success is True
        assert mock_connect.call_args.kwargs["password"] == "super_secret_password"

    # With None password (defaults to empty string)
    with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
        res = connector.test_connection(conn_config, password=None)
        assert res.success is True
        assert mock_connect.call_args.kwargs["password"] == ""


def test_mysql_connector_verification_query_failed() -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MySQLConnection(database="db", username="user")
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (0,)  # Expected 1, got 0
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("pymysql.connect", return_value=mock_conn):
        with pytest.raises(GeneralConnectionError, match="Database verification query failed"):
            connector.test_connection(conn_config)

    mock_conn.close.assert_called_once()


def test_mysql_connector_resource_cleanup_on_error() -> None:
    connector = MySQLCompatibleConnector()
    conn_config = MySQLConnection(database="db", username="user")
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = RuntimeError("Query execution crashed")
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    with patch("pymysql.connect", return_value=mock_conn):
        with pytest.raises(GeneralConnectionError, match="Query execution crashed"):
            connector.test_connection(conn_config)

    mock_conn.close.assert_called_once()


def test_mysql_connector_error_mappings() -> None:
    connector = MySQLCompatibleConnector(timeout_seconds=10.0)
    conn_config = MySQLConnection(database="app", username="appuser", host="db.local", port=3306)

    # 1. Authentication failure (1045 or SQLSTATE 28000)
    auth_err_1045 = pymysql.err.OperationalError(ER.ACCESS_DENIED_ERROR, "Access denied for user 'appuser'@'db.local'")
    auth_err_1045.sqlstate = "28000"
    with patch("pymysql.connect", side_effect=auth_err_1045):
        with pytest.raises(AuthenticationFailedError, match="Authentication failed for user 'appuser'"):
            connector.test_connection(conn_config)

    auth_err_28000 = pymysql.err.OperationalError(9998, "Generic authentication failed")
    auth_err_28000.sqlstate = "28000"
    with patch("pymysql.connect", side_effect=auth_err_28000):
        with pytest.raises(AuthenticationFailedError, match="Authentication failed for user 'appuser'"):
            connector.test_connection(conn_config)

    # 2. Database access denied (1044) - must NOT be classified as AuthenticationFailedError
    db_access_err = pymysql.err.OperationalError(
        ER.DBACCESS_DENIED_ERROR,
        "Access denied for user 'appuser'@'db.local' to database 'app'",
    )
    db_access_err.sqlstate = "42000"
    with patch("pymysql.connect", side_effect=db_access_err):
        with pytest.raises(GeneralConnectionError, match="Access denied for user 'appuser' to database 'app'") as exc_info:
            connector.test_connection(conn_config)
        # Ensure it is NOT an AuthenticationFailedError
        assert not isinstance(exc_info.value, AuthenticationFailedError)
        assert "lacks access privileges to this database" in str(exc_info.value)

    # 3. Database not found (1049)
    db_err_1049 = pymysql.err.OperationalError(ER.BAD_DB_ERROR, "Unknown database 'app'")
    db_err_1049.sqlstate = "42000"
    with patch("pymysql.connect", side_effect=db_err_1049):
        with pytest.raises(DatabaseNotFoundError, match="Database 'app' does not exist on host 'db.local:3306'"):
            connector.test_connection(conn_config)

    # 4. Generic SQLSTATE 42000 (e.g. syntax error 1064) - must NOT be treated as DatabaseNotFoundError
    syntax_err = pymysql.err.ProgrammingError(1064, "You have an error in your SQL syntax")
    syntax_err.sqlstate = "42000"
    with patch("pymysql.connect", side_effect=syntax_err):
        with pytest.raises(GeneralConnectionError, match="Failed to connect to 'db.local:3306'") as exc_info:
            connector.test_connection(conn_config)
        # Ensure it is NOT treated as DatabaseNotFoundError
        assert not isinstance(exc_info.value, DatabaseNotFoundError)

    # 5. Unknown host / resolution failure
    host_err = pymysql.err.OperationalError(CR.CR_UNKNOWN_HOST, "Unknown MySQL server host 'db.local'")
    with patch("pymysql.connect", side_effect=host_err):
        with pytest.raises(HostUnreachableError, match="Could not resolve host 'db.local'"):
            connector.test_connection(conn_config)

    # 4. Connection refused
    refused_err = pymysql.err.OperationalError(CR.CR_CONN_HOST_ERROR, "Can't connect to MySQL server")
    refused_err.original_exception = ConnectionRefusedError(111, "Connection refused")
    with patch("pymysql.connect", side_effect=refused_err):
        with pytest.raises(HostUnreachableError, match="Connection refused by 'db.local:3306'"):
            connector.test_connection(conn_config)

    # 5. Connection timeout
    timeout_err = pymysql.err.OperationalError(CR.CR_CONN_HOST_ERROR, "Can't connect to MySQL server")
    timeout_err.original_exception = socket.timeout("timed out")
    with patch("pymysql.connect", side_effect=timeout_err):
        with pytest.raises(ConnectionTimeoutError, match="Connection timed out after 10 seconds"):
            connector.test_connection(conn_config)

    # 6. SSL verification failure
    ssl_err = pymysql.err.OperationalError(CR.CR_SSL_CONNECTION_ERROR, "SSL connection error: certificate verify failed")
    ssl_err.original_exception = ssl.SSLCertVerificationError("certificate verify failed")
    with patch("pymysql.connect", side_effect=ssl_err):
        with pytest.raises(SSLVerificationError, match="SSL/TLS verification failed connecting to 'db.local:3306'"):
            connector.test_connection(conn_config)

    # 7. Unclassified general connection error
    gen_err = pymysql.err.InternalError(9999, "Some internal MySQL engine anomaly")
    with patch("pymysql.connect", side_effect=gen_err):
        with pytest.raises(GeneralConnectionError, match="Failed to connect to 'db.local:3306'"):
            connector.test_connection(conn_config)


def test_mariadb_error_1044_database_access_denied() -> None:
    connector = MySQLCompatibleConnector()
    maria_config = MariaDBConnection(database="analytics", username="maria_user")
    err_1044 = pymysql.err.OperationalError(
        ER.DBACCESS_DENIED_ERROR,
        "Access denied for user 'maria_user'@'localhost' to database 'analytics'",
    )
    with patch("pymysql.connect", side_effect=err_1044):
        with pytest.raises(GeneralConnectionError, match="Access denied for user 'maria_user' to database 'analytics'") as exc_info:
            connector.test_connection(maria_config)
        assert not isinstance(exc_info.value, AuthenticationFailedError)
        assert "lacks access privileges to this database" in str(exc_info.value)

