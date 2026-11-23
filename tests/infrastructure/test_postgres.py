"""Tests for PostgresConnector infrastructure component."""

from unittest.mock import MagicMock, patch
import pytest

psycopg = pytest.importorskip("psycopg")
import psycopg.errors

from support.application.errors import (
    AuthenticationFailedError,
    ConnectionTimeoutError,
    DatabaseNotFoundError,
    GeneralConnectionError,
    HostUnreachableError,
    SSLVerificationError,
)
from support.domain.profiles import PostgresConnection
from support.infrastructure.postgres import PostgresConnector


def _make_connection(**kwargs) -> PostgresConnection:
    defaults = {
        "database": "test_db",
        "username": "test_user",
        "host": "db.example.com",
        "port": 5433,
        "ssl_mode": "require",
    }
    defaults.update(kwargs)
    return PostgresConnection(**defaults)


def test_postgres_connector_passes_exact_parameters() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector(connect_timeout=10)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.closed = False
    mock_conn.info.parameter_status.return_value = "16.2"

    with patch("psycopg.connect", return_value=mock_conn) as mock_connect:
        result = connector.test_connection(conn_config, password="secure-pass-123")

        mock_connect.assert_called_once_with(
            host="db.example.com",
            port=5433,
            dbname="test_db",
            user="test_user",
            sslmode="require",
            connect_timeout=10,
            password="secure-pass-123",
        )
        mock_cursor.execute.assert_called_once_with("SELECT 1")
        assert result.success is True
        assert result.database == "test_db"
        assert result.username == "test_user"
        assert result.host == "db.example.com"
        assert result.port == 5433
        assert result.ssl_mode == "require"
        assert result.server_version == "16.2"
        mock_conn.close.assert_called_once()


def test_postgres_connector_omits_password_when_none() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector()

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.closed = False
    mock_conn.info.parameter_status.return_value = None
    mock_conn.info.server_version = 150001

    with patch("psycopg.connect", return_value=mock_conn) as mock_connect:
        result = connector.test_connection(conn_config, password=None)

        # Ensure 'password' key is NOT in kwargs
        assert "password" not in mock_connect.call_args.kwargs
        assert result.server_version == "15.1"
        mock_conn.close.assert_called_once()


def test_postgres_connector_closes_connection_on_query_failure() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector()

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = psycopg.errors.OperationalError("Query failed")
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.closed = False

    with patch("psycopg.connect", return_value=mock_conn):
        with pytest.raises(GeneralConnectionError):
            connector.test_connection(conn_config)

        mock_conn.close.assert_called_once()


def test_error_translation_authentication_failed() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector()

    # 1. InvalidPassword exception
    with patch("psycopg.connect", side_effect=psycopg.errors.InvalidPassword("password authentication failed")):
        with pytest.raises(AuthenticationFailedError, match="Authentication failed for user 'test_user'"):
            connector.test_connection(conn_config, password="bad")

    # 2. OperationalError with password authentication failed
    with patch("psycopg.connect", side_effect=psycopg.OperationalError("FATAL: password authentication failed for user")):
        with pytest.raises(AuthenticationFailedError, match="Authentication failed for user 'test_user'"):
            connector.test_connection(conn_config, password="bad")


def test_error_translation_database_not_found() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector()

    with patch("psycopg.connect", side_effect=psycopg.errors.InvalidCatalogName('database "test_db" does not exist')):
        with pytest.raises(DatabaseNotFoundError, match="Database 'test_db' does not exist"):
            connector.test_connection(conn_config)


def test_error_translation_connection_refused() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector()

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("connection to server at '127.0.0.1', port 5433 failed: Connection refused")):
        with pytest.raises(HostUnreachableError, match="Connection refused by 'db.example.com:5433'"):
            connector.test_connection(conn_config)


def test_error_translation_connection_timeout() -> None:
    conn_config = _make_connection()
    connector = PostgresConnector(connect_timeout=10)

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("connection timed out")):
        with pytest.raises(ConnectionTimeoutError, match="Connection timed out after 10 seconds"):
            connector.test_connection(conn_config)


def test_error_translation_ssl_verification() -> None:
    conn_config = _make_connection(ssl_mode="verify-full")
    connector = PostgresConnector()

    with patch("psycopg.connect", side_effect=psycopg.OperationalError("SSL error: certificate verify failed")):
        with pytest.raises(SSLVerificationError, match="SSL/TLS verification failed"):
            connector.test_connection(conn_config)
