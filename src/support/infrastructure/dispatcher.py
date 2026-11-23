"""Database connector dispatcher routing connection configs to driver adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING
from support.application.errors import (
    GeneralConnectionError,
    UnsupportedDatabaseDriverError,
)
from support.application.ports import ConnectionTestResult, DatabaseConnector, SchemaIntrospector
from support.domain.profiles import (
    ConnectionConfig,
    DatabaseType,
    MariaDBConnection,
    MongoDBConnection,
    MySQLConnection,
    PostgresConnection,
    SQLiteConnection,
    SQLServerConnection,
)
from support.domain.schema import DatabaseSchema, TableMetadata

if TYPE_CHECKING:
    from support.infrastructure.mongodb import MongoDBConnector
    from support.infrastructure.mysql_connector import MySQLCompatibleConnector
    from support.infrastructure.postgres import PostgresConnector
    from support.infrastructure.sqlite_connector import SQLiteConnector
    from support.infrastructure.sqlserver import SQLServerConnector


class ConnectorDispatcher(DatabaseConnector, SchemaIntrospector):
    """Explicit dispatcher routing connection configurations to their respective connectors."""

    def __init__(
        self,
        postgres_connector: PostgresConnector | None = None,
        sqlite_connector: SQLiteConnector | None = None,
        mysql_connector: MySQLCompatibleConnector | None = None,
        sqlserver_connector: SQLServerConnector | None = None,
        mongodb_connector: MongoDBConnector | None = None,
    ) -> None:
        self._postgres_connector = postgres_connector
        self._sqlite_connector = sqlite_connector
        self._mysql_connector = mysql_connector
        self._sqlserver_connector = sqlserver_connector
        self._mongodb_connector = mongodb_connector

    def _get_postgres_connector(self) -> DatabaseConnector:
        if self._postgres_connector is None:
            try:
                from support.infrastructure.postgres import PostgresConnector
            except ModuleNotFoundError as exc:
                if exc.name in ("psycopg", "psycopg_binary"):
                    raise UnsupportedDatabaseDriverError(
                        "PostgreSQL driver 'psycopg' is not installed. "
                        'Install it using: pip install "support-db[postgres]"'
                    ) from exc
                raise GeneralConnectionError(
                    f"Failed to load PostgreSQL driver due to missing dependency or library: {exc}"
                ) from exc
            except ImportError as exc:
                raise GeneralConnectionError(
                    f"Failed to load PostgreSQL driver due to import error: {exc}"
                ) from exc

            self._postgres_connector = PostgresConnector()
        return self._postgres_connector

    def _get_sqlite_connector(self) -> DatabaseConnector:
        if self._sqlite_connector is None:
            from support.infrastructure.sqlite_connector import SQLiteConnector

            self._sqlite_connector = SQLiteConnector()
        return self._sqlite_connector

    def _get_mysql_connector(self) -> DatabaseConnector:
        if self._mysql_connector is None:
            try:
                from support.infrastructure.mysql_connector import MySQLCompatibleConnector
            except ModuleNotFoundError as exc:
                if exc.name == "pymysql":
                    raise UnsupportedDatabaseDriverError(
                        "MySQL driver 'PyMySQL' is not installed. "
                        'Install it using: pip install "support-db[mysql]"'
                    ) from exc
                raise GeneralConnectionError(
                    f"Failed to load MySQL driver due to missing dependency or library: {exc}"
                ) from exc
            except ImportError as exc:
                raise GeneralConnectionError(
                    f"Failed to load MySQL driver due to import error: {exc}"
                ) from exc

            self._mysql_connector = MySQLCompatibleConnector()
        return self._mysql_connector

    def _get_sqlserver_connector(self) -> DatabaseConnector:
        if self._sqlserver_connector is None:
            try:
                import mssql_python
            except ModuleNotFoundError as exc:
                if exc.name in ("mssql_python", "mssql-python"):
                    raise UnsupportedDatabaseDriverError(
                        "SQL Server driver 'mssql-python' is not installed. "
                        'Install it using: pip install "support-db[sqlserver]"'
                    ) from exc
                raise GeneralConnectionError(
                    f"Failed to load SQL Server driver due to missing dependency or library: {exc}"
                ) from exc
            except ImportError as exc:
                raise GeneralConnectionError(
                    f"Failed to load SQL Server driver due to import error: {exc}"
                ) from exc

            from support.infrastructure.sqlserver import SQLServerConnector

            self._sqlserver_connector = SQLServerConnector()
        return self._sqlserver_connector

    def _get_mongodb_connector(self) -> DatabaseConnector:
        if self._mongodb_connector is None:
            try:
                import pymongo
            except ModuleNotFoundError as exc:
                if exc.name == "pymongo":
                    raise UnsupportedDatabaseDriverError(
                        "MongoDB driver 'pymongo' is not installed. "
                        'Install it using: pip install "support-db[mongodb]"'
                    ) from exc
                raise GeneralConnectionError(
                    f"Failed to load MongoDB driver due to missing dependency or library: {exc}"
                ) from exc
            except ImportError as exc:
                raise GeneralConnectionError(
                    f"Failed to load MongoDB driver due to import error: {exc}"
                ) from exc

            from support.infrastructure.mongodb import MongoDBConnector

            self._mongodb_connector = MongoDBConnector()
        return self._mongodb_connector

    def _route_connector(self, connection: ConnectionConfig):
        """Route connection configuration to its respective connector instance."""
        if isinstance(connection, PostgresConnection):
            return self._get_postgres_connector()
        elif isinstance(connection, SQLiteConnection):
            return self._get_sqlite_connector()
        elif isinstance(connection, (MySQLConnection, MariaDBConnection)):
            return self._get_mysql_connector()
        elif isinstance(connection, SQLServerConnection):
            return self._get_sqlserver_connector()
        elif isinstance(connection, MongoDBConnection):
            return self._get_mongodb_connector()

        db_type = getattr(connection, "database_type", None)
        type_str = db_type.value if isinstance(db_type, DatabaseType) else type(connection).__name__
        raise UnsupportedDatabaseDriverError(
            f"Connection testing is not yet supported for database type '{type_str}'."
        )

    def test_connection(
        self,
        connection: ConnectionConfig,
        password: str | None = None,
    ) -> ConnectionTestResult:
        """Route the connection configuration to the appropriate database connector."""
        connector = self._route_connector(connection)
        return connector.test_connection(connection, password=password)

    def introspect_schema(
        self,
        connection: ConnectionConfig,
        password: str | None = None,
    ) -> DatabaseSchema:
        """Introspect database schema using the appropriate connector."""
        connector = self._route_connector(connection)
        return connector.introspect_schema(connection, password=password)

    def introspect_table(
        self,
        connection: ConnectionConfig,
        table_name: str,
        schema: str | None = None,
        password: str | None = None,
    ) -> TableMetadata:
        """Introspect a specific database table or collection using the appropriate connector."""
        connector = self._route_connector(connection)
        return connector.introspect_table(connection, table_name=table_name, schema=schema, password=password)
