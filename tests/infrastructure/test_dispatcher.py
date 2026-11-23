"""Unit tests for ConnectorDispatcher."""

from unittest.mock import MagicMock
import pytest

from support.application.errors import UnsupportedDatabaseDriverError
from support.application.ports import ConnectionTestResult
from support.domain.profiles import (
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)
from support.infrastructure.dispatcher import ConnectorDispatcher


def test_dispatcher_routes_postgres_connection() -> None:
    mock_pg = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="postgresql",
        database="testdb",
        username="postgres",
        host="localhost",
        port=5432,
        ssl_mode="prefer",
    )
    mock_pg.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(postgres_connector=mock_pg)
    conn = PostgresConnection(database="testdb", username="postgres", ssl_mode="prefer")

    result = dispatcher.test_connection(conn, password="secret")
    assert result == expected_result
    mock_pg.test_connection.assert_called_once_with(conn, password="secret")


def test_dispatcher_routes_sqlite_connection() -> None:
    mock_sqlite = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="sqlite",
        database="test.db",
        server_version="3.45.0",
    )
    mock_sqlite.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(sqlite_connector=mock_sqlite)
    conn = SQLiteConnection(database_path="/path/test.db")

    result = dispatcher.test_connection(conn)
    assert result == expected_result
    mock_sqlite.test_connection.assert_called_once_with(conn, password=None)


def test_dispatcher_routes_mysql_and_mariadb_connection() -> None:
    mock_mysql = MagicMock()
    expected_result = ConnectionTestResult(
        success=True,
        database_type="mysql",
        database="shopdb",
        username="admin",
        host="localhost",
        port=3306,
        server_version="8.0.35",
    )
    mock_mysql.test_connection.return_value = expected_result

    dispatcher = ConnectorDispatcher(mysql_connector=mock_mysql)
    mysql_conn = MySQLConnection(database="shopdb", username="admin")
    result = dispatcher.test_connection(mysql_conn, password="pass")
    assert result == expected_result
    mock_mysql.test_connection.assert_called_once_with(mysql_conn, password="pass")

    mock_mysql.reset_mock()
    mariadb_conn = MariaDBConnection(database="shopdb", username="admin")
    dispatcher.test_connection(mariadb_conn, password="pass")
    mock_mysql.test_connection.assert_called_once_with(mariadb_conn, password="pass")


def test_dispatcher_rejects_unsupported_database_type() -> None:
    dispatcher = ConnectorDispatcher()

    sqlserver_conn = SQLServerConnection(database="testdb", username="sa")
    with pytest.raises(
        UnsupportedDatabaseDriverError,
        match="Connection testing is not yet supported for database type 'sqlserver'",
    ):
        dispatcher.test_connection(sqlserver_conn)

    mongo_conn = MongoDBConnection(database="testdb")
    with pytest.raises(
        UnsupportedDatabaseDriverError,
        match="Connection testing is not yet supported for database type 'mongodb'",
    ):
        dispatcher.test_connection(mongo_conn)
