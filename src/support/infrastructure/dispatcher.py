"""Database connector dispatcher routing connection configs to driver adapters."""

from support.application.errors import UnsupportedDatabaseDriverError
from support.application.ports import ConnectionTestResult, DatabaseConnector
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseType,
    MariaDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
)
from support.infrastructure.mysql_connector import MySQLCompatibleConnector
from support.infrastructure.postgres import PostgresConnector
from support.infrastructure.sqlite_connector import SQLiteConnector


class ConnectorDispatcher(DatabaseConnector):
    """Explicit dispatcher routing connection configurations to their respective connectors."""

    def __init__(
        self,
        postgres_connector: PostgresConnector | None = None,
        sqlite_connector: SQLiteConnector | None = None,
        mysql_connector: MySQLCompatibleConnector | None = None,
    ) -> None:
        self._postgres_connector = postgres_connector or PostgresConnector()
        self._sqlite_connector = sqlite_connector or SQLiteConnector()
        self._mysql_connector = mysql_connector or MySQLCompatibleConnector()

    def test_connection(
        self,
        connection: ConnectionConfig,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Route the connection configuration to the appropriate database connector."""
        if isinstance(connection, PostgresConnection):
            return self._postgres_connector.test_connection(connection, password=password)
        elif isinstance(connection, SQLiteConnection):
            return self._sqlite_connector.test_connection(connection, password=password)
        elif isinstance(connection, (MySQLConnection, MariaDBConnection)):
            return self._mysql_connector.test_connection(connection, password=password)

        db_type = getattr(connection, "database_type", None)
        type_str = db_type.value if isinstance(db_type, DatabaseType) else type(connection).__name__
        raise UnsupportedDatabaseDriverError(
            f"Connection testing is not yet supported for database type '{type_str}'."
        )
