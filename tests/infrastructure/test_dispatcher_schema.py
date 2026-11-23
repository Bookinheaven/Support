"""Unit tests for ConnectorDispatcher schema routing."""

from unittest.mock import MagicMock
import pytest

from support.domain.profiles import (
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)
from support.domain.schema import DatabaseSchema, TableMetadata
from support.infrastructure.dispatcher import ConnectorDispatcher


def test_dispatcher_routes_schema_introspection_to_connectors() -> None:
    mock_pg = MagicMock()
    mock_sqlite = MagicMock()
    mock_mysql = MagicMock()
    mock_sqlserver = MagicMock()
    mock_mongo = MagicMock()

    dispatcher = ConnectorDispatcher(
        postgres_connector=mock_pg,
        sqlite_connector=mock_sqlite,
        mysql_connector=mock_mysql,
        sqlserver_connector=mock_sqlserver,
        mongodb_connector=mock_mongo,
    )

    # Postgres
    pg_conn = PostgresConnection(database="pgdb", username="u", ssl_mode="prefer")
    dispatcher.introspect_schema(pg_conn, password="pw")
    mock_pg.introspect_schema.assert_called_once_with(pg_conn, password="pw")

    # SQLite
    sq_conn = SQLiteConnection(database_path="/tmp/db.sqlite")
    dispatcher.introspect_schema(sq_conn)
    mock_sqlite.introspect_schema.assert_called_once_with(sq_conn, password=None)

    # MySQL
    my_conn = MySQLConnection(database="mydb", username="root", ssl_mode="disabled")
    dispatcher.introspect_schema(my_conn, password="pw")
    mock_mysql.introspect_schema.assert_called_once_with(my_conn, password="pw")

    # MariaDB
    maria_conn = MariaDBConnection(database="mariadb", username="root", ssl_mode="disabled")
    dispatcher.introspect_schema(maria_conn, password="pw")
    mock_mysql.introspect_schema.assert_called_with(maria_conn, password="pw")

    # SQL Server
    mssql_conn = SQLServerConnection(database="msdb", username="sa")
    dispatcher.introspect_schema(mssql_conn, password="pw")
    mock_sqlserver.introspect_schema.assert_called_once_with(mssql_conn, password="pw")

    # MongoDB
    mongo_conn = MongoDBConnection(uri="mongodb://localhost:27017", database="mgdb")
    dispatcher.introspect_schema(mongo_conn)
    mock_mongo.introspect_schema.assert_called_once_with(mongo_conn, password=None)


def test_dispatcher_routes_table_introspection_to_connectors() -> None:
    mock_pg = MagicMock()
    dispatcher = ConnectorDispatcher(postgres_connector=mock_pg)
    pg_conn = PostgresConnection(database="pgdb", username="u", ssl_mode="prefer")

    dispatcher.introspect_table(pg_conn, "users", schema="public", password="pw")
    mock_pg.introspect_table.assert_called_once_with(
        pg_conn,
        table_name="users",
        schema="public",
        password="pw",
    )
