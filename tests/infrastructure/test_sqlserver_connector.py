"""Unit tests for SQLServerConnector using mocked mssql-python."""

from unittest.mock import MagicMock, patch
import pytest

mssql_python = pytest.importorskip("mssql_python")

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    SSLVerificationError,
)
from support.domain.profiles import SQLServerConnection
from support.infrastructure.sqlserver import SQLServerConnector


def _make_connection(**kwargs) -> SQLServerConnection:
    defaults = {
        "database": "test_db",
        "username": "sa",
        "host": "localhost",
        "port": 1433,
        "encrypt": True,
        "trust_server_certificate": False,
    }
    defaults.update(kwargs)
    return SQLServerConnection(**defaults)


def test_sqlserver_parameter_mapping_and_select_1() -> None:
    connector = SQLServerConnector(login_timeout=10, statement_timeout=10)
    conn_model = _make_connection(
        database="production",
        username="admin",
        host="sql.example.com",
        port=14333,
        encrypt=True,
        trust_server_certificate=False,
    )

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_conn.cursor.return_value = mock_cursor

    with patch("mssql_python.pooling") as mock_pooling, patch(
        "mssql_python.connect", return_value=mock_conn
    ) as mock_connect:
        result = connector.test_connection(conn_model, password="super-secret-password")

        # 1. Pooling disabled before connect
        mock_pooling.assert_called_once_with(enabled=False)

        # 2. Parameters mapped correctly
        mock_connect.assert_called_once_with(
            Server="sql.example.com,14333",
            Database="production",
            UID="admin",
            PWD="super-secret-password",
            Encrypt="yes",
            TrustServerCertificate="no",
            timeout=10,
            autocommit=True,
        )

        # 3. Statement timeout set on connection
        assert mock_conn.timeout == 10

        # 4. SELECT 1 query executed and verified
        mock_cursor.execute.assert_called_once_with("SELECT 1")
        mock_cursor.fetchone.assert_called_once()

        # 5. Cursor and connection cleanup
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

        # 6. Result fields
        assert result.success is True
        assert result.database_type == "sqlserver"
        assert result.database == "production"
        assert result.username == "admin"
        assert result.host == "sql.example.com"
        assert result.port == 14333
        assert result.server_version is None


def test_sqlserver_tls_configuration_mapping() -> None:
    connector = SQLServerConnector()

    # Case 1: encrypt=False, trust_server_certificate=True
    conn1 = _make_connection(encrypt=False, trust_server_certificate=True)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.fetchone.return_value = (1,)

    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn1, password="p")
        assert mock_connect.call_args.kwargs["Encrypt"] == "no"
        assert mock_connect.call_args.kwargs["TrustServerCertificate"] == "yes"

    # Case 2: encrypt=True, trust_server_certificate=True
    conn2 = _make_connection(encrypt=True, trust_server_certificate=True)
    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn2, password="p")
        assert mock_connect.call_args.kwargs["Encrypt"] == "yes"
        assert mock_connect.call_args.kwargs["TrustServerCertificate"] == "yes"


def test_sqlserver_password_delivery_without_secrets() -> None:
    connector = SQLServerConnector()
    conn_model = _make_connection()

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.fetchone.return_value = (1,)

    # With None password (no PWD passed)
    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn_model, password=None)
        assert "PWD" not in mock_connect.call_args.kwargs

    # With explicit password
    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn) as mock_connect:
        connector.test_connection(conn_model, password="my-secret-token")
        assert mock_connect.call_args.kwargs["PWD"] == "my-secret-token"


def test_sqlserver_cleanup_on_query_failure() -> None:
    connector = SQLServerConnector()
    conn_model = _make_connection()

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = RuntimeError("Crash during SELECT 1")
    mock_conn.cursor.return_value = mock_cursor

    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn):
        with pytest.raises(GeneralConnectionError):
            connector.test_connection(conn_model, password="secret")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


def test_sqlserver_unexpected_select_1_result() -> None:
    connector = SQLServerConnector()
    conn_model = _make_connection()

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (0,)  # unexpected value
    mock_conn.cursor.return_value = mock_cursor

    with patch("mssql_python.pooling"), patch("mssql_python.connect", return_value=mock_conn):
        with pytest.raises(GeneralConnectionError, match="unexpected result"):
            connector.test_connection(conn_model, password="secret")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class _FakeMssqlError(Exception):
    def __init__(self, driver_error: str = "", ddbc_error: str = "", sqlstate: str = ""):
        self.driver_error = driver_error
        self.ddbc_error = ddbc_error
        self.sqlstate = sqlstate
        super().__init__(f"Driver Error: {driver_error}; DDBC Error: {ddbc_error}")


def test_sqlserver_error_translations() -> None:
    connector = SQLServerConnector()
    conn_model = _make_connection()

    # 1. Authentication failure (SQLSTATE 28000 or Login failed)
    err_auth = _FakeMssqlError(
        driver_error="Invalid authorization specification",
        ddbc_error="[Microsoft]Login failed for user 'sa'. (18456)",
        sqlstate="28000",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_auth):
        with pytest.raises(AuthenticationFailedError, match="Authentication failed for user 'sa'"):
            connector.test_connection(conn_model, password="badpass")

    # 2. Invalid catalog name (SQLSTATE 3D000)
    err_catalog = _FakeMssqlError(
        driver_error="Invalid catalog name",
        ddbc_error="[Microsoft]Catalog does not exist.",
        sqlstate="3D000",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_catalog):
        with pytest.raises(DatabaseNotFoundError, match="Database 'test_db' does not exist"):
            connector.test_connection(conn_model, password="pass")

    # 3. Database open / access failure (SQL Server error 4060 / Cannot open database)
    # Must be classified as GeneralConnectionError, NOT DatabaseNotFoundError or AuthenticationFailedError
    err_db_open = _FakeMssqlError(
        driver_error="Cannot open database",
        ddbc_error="[Microsoft]Cannot open database 'test_db' requested by the login.",
        sqlstate="42000",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_db_open):
        with pytest.raises(
            GeneralConnectionError,
            match="The requested database could not be opened\\. Verify that it exists and that the login has permission to access it\\.",
        ):
            connector.test_connection(conn_model, password="pass")

    # 3. Connection timeout (SQLSTATE HYT00 or HYT01)
    err_timeout = _FakeMssqlError(
        driver_error="Timeout expired",
        ddbc_error="[Microsoft]Login timeout expired",
        sqlstate="HYT00",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_timeout):
        with pytest.raises(ConnectionTimeoutError, match="Connection timed out connecting to"):
            connector.test_connection(conn_model, password="pass")

    # 4. SSL verification failure
    err_ssl = _FakeMssqlError(
        driver_error="Client unable to establish connection",
        ddbc_error="[Microsoft]SSL Provider: The certificate chain was issued by an authority that is not trusted.",
        sqlstate="08001",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_ssl):
        with pytest.raises(SSLVerificationError, match="TLS/SSL verification failed connecting to"):
            connector.test_connection(conn_model, password="pass")

    # 5. Network / server unreachable (SQLSTATE 08001 without SSL hint)
    err_unreach = _FakeMssqlError(
        driver_error="Client unable to establish connection",
        ddbc_error="[Microsoft]TCP Provider: Error code 0x2749",
        sqlstate="08001",
    )
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_unreach):
        with pytest.raises(HostUnreachableError, match="Could not connect to SQL Server"):
            connector.test_connection(conn_model, password="pass")

    # 6. General fallback error (and ensure password is sanitized if present in message)
    err_general = Exception("Unknown connection fault occurred with secret password 'mysecret'")
    with patch("mssql_python.pooling"), patch("mssql_python.connect", side_effect=err_general):
        with pytest.raises(GeneralConnectionError) as exc_info:
            connector.test_connection(conn_model, password="mysecret")
        assert "mysecret" not in str(exc_info.value)
        assert "******" in str(exc_info.value)
